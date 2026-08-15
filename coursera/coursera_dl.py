#!/usr/bin/env python
# -*- coding: utf-8 -*-

# Authors and copyright:
#     © 2012-2013, John Lehmann (first last at geemail dotcom or @jplehmann)
#     © 2012-2020, Rogério Theodoro de Brito
#     © 2013, Jonas De Taeye (first dt at fastmail fm)
#
# Contributions are welcome, but please add new unit tests to test your changes
# and/or features.  Also, please try to make changes platform independent and
# backward compatible.
#
# Legalese:
#
#  This program is free software: you can redistribute it and/or modify it
#  under the terms of the GNU Lesser General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or (at your
#  option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program.  If not, see <http://www.gnu.org/licenses/>.


"""
Module for downloading lecture resources such as videos for Coursera classes.

Given a class name, username and password, it scrapes the course listing
page to get the section (week) and lecture names, and then downloads the
related materials into appropriately named files and directories.

Examples:
  coursera-dl -u <user> -p <passwd> saas
  coursera-dl -u <user> -p <passwd> -l listing.html -o saas --skip-download

For further documentation and examples, visit the project's home at:
  https://github.com/coursera-dl/coursera
"""


import json
import logging
import os
import re
import time
import shutil

from distutils.version import LooseVersion as V


# Test versions of some critical modules.
# We may, perhaps, want to move these elsewhere.
import bs4
import six
import requests

from .cookies import (
    AuthenticationFailed, ClassNotFound,
    get_cookies_for_class, get_cookie_jar, make_cookie_values, TLSAdapter, login)
from .define import (CLASS_URL, ABOUT_URL, PATH_CACHE)
from .downloaders import get_downloader
from .workflow import CourseraDownloader
from .parallel import ConsecutiveDownloader, ParallelDownloader
from .utils import (clean_filename, get_anchor_format, mkdir_p, fix_url,
                    print_ssl_error_message,
                    decode_input, BeautifulSoup, is_debug_run,
                    spit_json, slurp_json)

from .api import expand_specializations
from .network import get_page, get_page_and_url
from .commandline import parse_args
from .extractors import CourseraExtractor

from coursera import __version__


# URL containing information about outdated modules
_SEE_URL = " See https://github.com/coursera-dl/coursera/issues/139"

assert V(requests.__version__) >= V('2.4'), "Upgrade requests!" + _SEE_URL
assert V(six.__version__) >= V('1.5'), "Upgrade six!" + _SEE_URL
assert V(bs4.__version__) >= V('4.1'), "Upgrade bs4!" + _SEE_URL


def get_session():
    """
    Create a bare session with TLS v1.2 certificate (no auth applied).
    Prefer build_session() when you also need authentication.
    """

    session = requests.Session()
    session.mount('https://', TLSAdapter())

    return session


def build_session(cookies_file=None, cauth=None, username=None, password=None):
    """
    Create an authenticated session ready for Coursera API calls.

    Exactly one auth source must be supplied:
      - cookies_file : path to a Netscape cookies.txt exported from a browser
      - cauth        : raw CAUTH cookie value
      - username + password : legacy login (no longer works on modern Coursera)

    This function is called both by main() (CLI path) and by the Gradio UI
    adapter so both share identical auth logic.

    Returns
    -------
    requests.Session
        Session with cookies and CSRF3 header set.
    """
    session = get_session()

    if cauth:
        session.cookies.set('CAUTH', cauth)
    elif cookies_file:
        jar = get_cookie_jar(cookies_file)
        session.cookies.update(jar)
        csrf3 = (session.cookies.get('CSRF3-Token')
                 or session.cookies.get('csrf3-token'))
        if csrf3:
            session.headers['X-CSRF3-Token'] = csrf3
    elif username and password:
        login(session, username, password)
    else:
        raise ValueError(
            'build_session() requires cookies_file, cauth, or username+password.'
        )

    return session


def list_courses(args):
    """
    List enrolled courses.

    @param args: Command-line arguments.
    @type args: namedtuple
    """
    session = get_session()
    login(session, args.username, args.password)
    extractor = CourseraExtractor(session)
    courses = extractor.list_courses()
    logging.info('Found %d courses', len(courses))
    for course in courses:
        logging.info(course)


def download_on_demand_class(session, args, class_name, folder_name=None):
    """
    Download all requested resources from the on-demand class given
    in class_name.

    @return: Tuple of (bool, bool), where the first bool indicates whether
        errors occurred while parsing syllabus, the second bool indicates
        whether the course appears to be completed.
    @rtype: (bool, bool)
    """

    error_occurred = False
    extractor = CourseraExtractor(session)

    cached_syllabus_filename = '%s-syllabus-parsed.json' % class_name
    if args.cache_syllabus and os.path.isfile(cached_syllabus_filename):
        modules = slurp_json(cached_syllabus_filename)
    else:
        error_occurred, modules = extractor.get_modules(
            class_name,
            args.reverse,
            args.unrestricted_filenames,
            args.subtitle_language,
            args.video_resolution,
            args.download_quizzes,
            args.mathjax_cdn_url,
            args.download_notebooks
        )

    if is_debug_run or args.cache_syllabus():
        spit_json(modules, cached_syllabus_filename)

    if args.only_syllabus:
        return error_occurred, False

    downloader = get_downloader(session, class_name, args)
    downloader_wrapper = ParallelDownloader(downloader, args.jobs) \
        if args.jobs > 1 else ConsecutiveDownloader(downloader)

    # obtain the resources

    ignored_formats = []
    if args.ignore_formats:
        ignored_formats = args.ignore_formats.split(",")

    course_downloader = CourseraDownloader(
        downloader_wrapper,
        commandline_args=args,
        class_name=folder_name or class_name,
        path=args.path,
        ignored_formats=ignored_formats,
        disable_url_skipping=args.disable_url_skipping
    )

    completed = course_downloader.download_modules(modules)

    # Print skipped URLs if any
    if course_downloader.skipped_urls:
        print_skipped_urls(course_downloader.skipped_urls)

    # Print failed URLs if any
    # FIXME: should we set non-zero exit code if we have failed URLs?
    if course_downloader.failed_urls:
        print_failed_urls(course_downloader.failed_urls)

    return error_occurred, completed


def print_skipped_urls(skipped_urls):
    logging.info('The following URLs (%d) have been skipped and not '
                 'downloaded:', len(skipped_urls))
    logging.info('(if you want to download these URLs anyway, please '
                 'add "--disable-url-skipping" option)')
    logging.info('-' * 80)
    for url in skipped_urls:
        logging.info(url)
    logging.info('-' * 80)


def print_failed_urls(failed_urls):
    logging.info('The following URLs (%d) could not be downloaded:',
                 len(failed_urls))
    logging.info('-' * 80)
    for url in failed_urls:
        logging.info(url)
    logging.info('-' * 80)


def download_class(session, args, class_name, folder_name=None):
    """
    Try to download on-demand class.

    @param folder_name: Optional override for the on-disk folder name.
        When provided the API still uses class_name; only the directory
        written to disk uses folder_name.

    @return: Tuple of (bool, bool), where the first bool indicates whether
        errors occurred while parsing syllabus, the second bool indicates
        whether the course appears to be completed.
    @rtype: (bool, bool)
    """
    logging.debug('Downloading new style (on demand) class %s', class_name)
    return download_on_demand_class(
        session, args, class_name, folder_name=folder_name or class_name)


def main():
    """
    Main entry point for execution as a program (instead of as a module).
    """

    args = parse_args()
    logging.info('coursera_dl version %s', __version__)
    completed_classes = []
    classes_with_errors = []

    mkdir_p(PATH_CACHE, 0o700)
    if args.clear_cache:
        shutil.rmtree(PATH_CACHE)
    if args.list_courses:
        logging.info('Listing enrolled courses')
        list_courses(args)
        return

    session = build_session(
        cookies_file=args.cookies_file,
        cauth=getattr(args, 'cookies_cauth', None),
        username=getattr(args, 'username', None),
        password=getattr(args, 'password', None),
    )
    if args.specialization:
        expanded = expand_specializations(session, args.class_names)
        if not args.assume_yes:
            import re as _re

            def _to_slug(text):
                """Convert a course name or slug to a valid URL slug."""
                text = text.lower()
                text = _re.sub(r'[^a-z0-9\s-]', '', text)  # strip special chars
                text = _re.sub(r'[\s]+', '-', text.strip())  # spaces → hyphens
                text = _re.sub(r'-+', '-', text).strip('-')  # collapse dashes
                return text

            # Show all resolved slugs at once, numbered.
            print('\nResolved %d course(s):' % len(expanded))
            for i, slug in enumerate(expanded, 1):
                print('  %d. %s' % (i, slug))
            print('\n  Tip: slugs are the URL part after coursera.org/learn/')
            print('  e.g. "build-train-and-deploy-ml-models-with-keras-on-google-cloud"')
            print('  You can also paste the full course name — it will be auto-converted.')

            # folder_map: api_slug -> folder name (defaults to api_slug)
            folder_map = {slug: slug for slug in expanded}

            all_ok = input('\nAre all slugs correct? [y/N] ').strip().lower()
            if all_ok in ('y', 'yes'):
                confirmed = list(expanded)
            else:
                # Ask which numbers are correct; fix the rest one by one.
                correct_input = input(
                    'Enter the numbers that ARE correct (e.g. 1,3,5), '
                    'or leave blank if none: '
                ).strip()
                correct_indices = set()
                if correct_input:
                    for part in correct_input.split(','):
                        part = part.strip()
                        if part.isdigit():
                            correct_indices.add(int(part))

                confirmed = list(expanded)
                for i, slug in enumerate(expanded, 1):
                    if i not in correct_indices:
                        raw = input(
                            'Course %d (was: %s)\n'
                            '  Enter the display name (from certificate page): '
                            % (i, slug)
                        ).strip()
                        if raw:
                            display_slug = _to_slug(raw)
                            # folder = "DisplayName_apiSlug" so both are visible
                            folder_map[slug] = '%s_%s' % (display_slug, slug)
                            print('  → API slug kept: %s' % slug)
                            print('  → Folder name : %s' % folder_map[slug])

            print('\nFinal course list to download:')
            for i, slug in enumerate(confirmed, 1):
                print('  %d. %s  (folder: %s)' % (i, slug, folder_map[slug]))
            answer = input('\nProceed with downloading these %d course(s)? [y/N] '
                           % len(confirmed)).strip().lower()
            if answer not in ('y', 'yes'):
                logging.info('Aborted by user.')
                return
            args.class_names = confirmed
            args.folder_map = folder_map
        else:
            args.class_names = expanded
            args.folder_map = {}

    for class_index, class_name in enumerate(args.class_names):
        try:
            folder_map = getattr(args, 'folder_map', {})
            folder_name = folder_map.get(class_name, class_name)
            logging.info('Downloading class: %s (%d / %d)',
                         folder_name, class_index + 1, len(args.class_names))
            error_occurred, completed = download_class(
                session, args, class_name, folder_name=folder_name)
            if completed:
                completed_classes.append(class_name)
            if error_occurred:
                classes_with_errors.append(class_name)
        except requests.exceptions.HTTPError as e:
            logging.error('HTTPError %s', e)
            if is_debug_run():
                logging.exception('HTTPError %s', e)
        except requests.exceptions.SSLError as e:
            logging.error('SSLError %s', e)
            print_ssl_error_message(e)
            if is_debug_run():
                raise
        except ClassNotFound as e:
            logging.error('Could not find class: %s', e)
        except AuthenticationFailed as e:
            logging.error('Could not authenticate: %s', e)

        if class_index + 1 != len(args.class_names):
            logging.info('Sleeping for %d seconds before downloading next course. '
                         'You can change this with --download-delay option.',
                         args.download_delay)
            time.sleep(args.download_delay)

    if completed_classes:
        logging.info('-' * 80)
        logging.info(
            "Classes which appear completed: " + " ".join(completed_classes))

    if classes_with_errors:
        logging.info('-' * 80)
        logging.info('The following classes had errors during the syllabus'
                     ' parsing stage. You may want to review error messages and'
                     ' courses (sometimes enrolling to the course or switching'
                     ' session helps):')
        for class_name in classes_with_errors:
            logging.info('%s (https://www.coursera.org/learn/%s)',
                         class_name, class_name)


if __name__ == '__main__':
    main()
