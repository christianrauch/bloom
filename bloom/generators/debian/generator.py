# Software License Agreement (BSD License)
#
# Copyright (c) 2013, Willow Garage, Inc.
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
#
#  * Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
#  * Redistributions in binary form must reproduce the above
#    copyright notice, this list of conditions and the following
#    disclaimer in the documentation and/or other materials provided
#    with the distribution.
#  * Neither the name of Willow Garage, Inc. nor the names of its
#    contributors may be used to endorse or promote products derived
#    from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS
# FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
# COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT,
# INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING,
# BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
# LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN
# ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

from __future__ import print_function

import collections
import datetime
import io
import json
import os
import pkg_resources
import re
import shutil
import sys
import traceback

# Python 2/3 support.
try:
    from configparser import ConfigParser
except ImportError:
    from ConfigParser import SafeConfigParser as ConfigParser
from dateutil import tz
from packaging.version import parse as parse_version

from bloom.generators import BloomGenerator
from bloom.generators import GeneratorError
from bloom.generators import resolve_dependencies
from bloom.generators import update_rosdep

from bloom.generators.common import default_fallback_resolver
from bloom.generators.common import invalidate_view_cache
from bloom.generators.common import evaluate_package_conditions
from bloom.generators.common import resolve_rosdep_key

from bloom.git import inbranch
from bloom.git import get_branches
from bloom.git import get_commit_hash
from bloom.git import get_current_branch
from bloom.git import has_changes
from bloom.git import show
from bloom.git import tag_exists

from bloom.logging import ansi
from bloom.logging import debug
from bloom.logging import enable_drop_first_log_prefix
from bloom.logging import error
from bloom.logging import fmt
from bloom.logging import info
from bloom.logging import is_debug
from bloom.logging import warning

from bloom.commands.git.patch.common import get_patch_config
from bloom.commands.git.patch.common import set_patch_config

from bloom.packages import get_package_data

from bloom.util import code
from bloom.util import to_unicode
from bloom.util import execute_command
from bloom.util import get_rfc_2822_date
from bloom.util import maybe_continue

try:
    from catkin_pkg.changelog import get_changelog_from_path
    from catkin_pkg.changelog import CHANGELOG_FILENAME
except ImportError as err:
    debug(traceback.format_exc())
    error("catkin_pkg was not detected, please install it.", exit=True)

try:
    import rosdistro
except ImportError as err:
    debug(traceback.format_exc())
    error("rosdistro was not detected, please install it.", exit=True)

try:
    import em
except ImportError:
    debug(traceback.format_exc())
    error("empy was not detected, please install it.", exit=True)

# Fix unicode bug in empy
# This should be removed once upstream empy is fixed
# See: https://github.com/ros-infrastructure/bloom/issues/196
try:
    em.str = unicode
    em.Stream.write_old = em.Stream.write
    em.Stream.write = lambda self, data: em.Stream.write_old(self, data.encode('utf8'))
except NameError:
    pass
# End fix

# Drop the first log prefix for this command
enable_drop_first_log_prefix(True)

TEMPLATE_EXTENSION = '.em'


def __place_template_folder(group, src, dst, gbp=False):
    template_files = pkg_resources.resource_listdir(group, src)
    # For each template, place
    for template_file in template_files:
        if not gbp and os.path.basename(template_file) == 'gbp.conf.em':
            debug("Skipping template '{0}'".format(template_file))
            continue
        template_path = os.path.join(src, template_file)
        template_dst = os.path.join(dst, template_file)
        if pkg_resources.resource_isdir(group, template_path):
            debug("Recursing on folder '{0}'".format(template_path))
            __place_template_folder(group, template_path, template_dst, gbp)
        else:
            try:
                debug("Placing template '{0}'".format(template_path))
                template = pkg_resources.resource_string(group, template_path)
                template_abs_path = pkg_resources.resource_filename(group, template_path)
            except IOError as err:
                error("Failed to load template "
                      "'{0}': {1}".format(template_file, str(err)), exit=True)
            if not os.path.exists(dst):
                os.makedirs(dst)
            if os.path.exists(template_dst):
                debug("Not overwriting existing file '{0}'".format(template_dst))
            else:
                with io.open(template_dst, 'w', encoding='utf-8') as f:
                    if not isinstance(template, str):
                        template = template.decode('utf-8')
                    # Python 2 API needs a `unicode` not a utf-8 string.
                    elif sys.version_info.major == 2:
                        template = template.decode('utf-8')
                    f.write(template)
                shutil.copystat(template_abs_path, template_dst)


def place_template_files(path, build_type, gbp=False):
    info(fmt("@!@{bf}==>@| Placing templates files in the 'debian' folder."))
    debian_path = os.path.join(path, 'debian')
    # Create/Clean the debian folder
    if not os.path.exists(debian_path):
        os.makedirs(debian_path)
    # Place template files
    group = 'bloom.generators.debian'
    templates = os.path.join('templates', build_type)
    __place_template_folder(group, templates, debian_path, gbp)


def summarize_dependency_mapping(data, deps, build_deps, resolved_deps):
    if len(deps) == 0 and len(build_deps) == 0:
        return
    info("Package '" + data['Package'] + "' has dependencies:")
    header = "  " + ansi('boldoff') + ansi('ulon') + \
             "rosdep key           => " + data['Distribution'] + \
             " key" + ansi('reset')
    template = "  " + ansi('cyanf') + "{0:<20} " + ansi('purplef') + \
               "=> " + ansi('cyanf') + "{1}" + ansi('reset')
    if len(deps) != 0:
        info(ansi('purplef') + "Run Dependencies:" +
             ansi('reset'))
        info(header)
        for key in [d.name for d in deps]:
            info(template.format(key, resolved_deps[key]))
    if len(build_deps) != 0:
        info(ansi('purplef') +
             "Build and Build Tool Dependencies:" + ansi('reset'))
        info(header)
        for key in [d.name for d in build_deps]:
            info(template.format(key, resolved_deps[key]))


def format_depends(depends, resolved_deps):
    versions = {
        'version_lt': '<<',
        'version_lte': '<=',
        'version_eq': '=',
        'version_gte': '>=',
        'version_gt': '>>'
    }
    formatted = []
    for d in depends:
        for resolved_dep in resolved_deps[d.name]:
            version_depends = [k
                               for k in versions.keys()
                               if getattr(d, k, None) is not None]
            if not version_depends:
                formatted.append(resolved_dep)
            else:
                for v in version_depends:
                    formatted.append("{0} ({1} {2})".format(
                        resolved_dep, versions[v], getattr(d, v)))
    return formatted


def format_description(value):
    """
    Format proper <synopsis, long desc> string following Debian control file
    formatting rules. Treat first line in given string as synopsis, everything
    else as a single, large paragraph.

    Future extensions of this function could convert embedded newlines and / or
    html into paragraphs in the Description field.

    https://www.debian.org/doc/debian-policy/ch-controlfields.html#s-f-Description
    """
    value = debianize_string(value)
    # NOTE: bit naive, only works for 'properly formatted' pkg descriptions (ie:
    #       'Text. Text'). Extra space to avoid splitting on arbitrary sequences
    #       of characters broken up by dots (version nrs fi).
    parts = value.split('. ', 1)
    if len(parts) == 1 or len(parts[1]) == 0:
        # most likely single line description
        return value
    # format according to rules in linked field documentation
    return u"{0}.\n {1}".format(parts[0], parts[1].strip())


def format_multiline(value):
    """
    Format multi-line text to appear in a Debian control file (or similar file)
    as a 'multiline' field type.

    https://www.debian.org/doc/debian-policy/ch-controlfields#syntax-of-control-files
    """
    # Insert a leading '.' if the first line is blank
    if value.startswith('\n'):
        value = '.' + value

    # Insert a trailing '.' if the last line is blank
    if value.endswith('\n'):
        value = value + '.'

    # Add a '.' to intermediate blank lines
    value = value.replace('\n\n', '\n.\n')
    # Once more, to handle consecutive blank lines
    value = value.replace('\n\n', '\n.\n')

    # Indent each additional line by a single space
    value = value.replace('\n', '\n ')

    return value


def get_changelogs(package, releaser_history=None):
    if releaser_history is None:
        warning("No historical releaser history, using current maintainer name "
                "and email for each versioned changelog entry.")
        releaser_history = {}
    if is_debug():
        import logging
        logging.basicConfig()
        import catkin_pkg
        catkin_pkg.changelog.log.setLevel(logging.DEBUG)
    package_path = os.path.abspath(os.path.dirname(package.filename))
    changelog_path = os.path.join(package_path, CHANGELOG_FILENAME)
    if os.path.exists(changelog_path):
        changelog = get_changelog_from_path(changelog_path)
        changelogs = []
        maintainer = (package.maintainers[0].name, package.maintainers[0].email)
        for version, date, changes in changelog.foreach_version(reverse=True):
            changes_str = []
            date_str = get_rfc_2822_date(date)
            for item in changes:
                changes_str.extend(['  ' + i for i in to_unicode(item).splitlines()])
            # Each entry has (version, date, changes, releaser, releaser_email)
            releaser, email = releaser_history.get(version, maintainer)
            changelogs.append((
                version, date_str, '\n'.join(changes_str), releaser, email
            ))
        return changelogs
    else:
        warning("No {0} found for package '{1}'"
                .format(CHANGELOG_FILENAME, package.name))
        return []


def missing_dep_resolver(key, peer_packages):
    if key in peer_packages:
        return [sanitize_package_name(key)]
    return default_fallback_resolver(key, peer_packages)


def generate_substitutions_from_package(
    package,
    os_name,
    os_version,
    ros_distro,
    installation_prefix='/usr',
    deb_inc=0,
    peer_packages=None,
    releaser_history=None,
    fallback_resolver=None,
    native=False
):
    peer_packages = peer_packages or []
    data = {}
    # Name, Version, Description
    data['Name'] = package.name
    data['Version'] = package.version
    data['Description'] = format_description(package.description)
    # Websites
    websites = [str(url) for url in package.urls if url.type == 'website']
    homepage = websites[0] if websites else ''
    if homepage == '':
        warning("No homepage set, defaulting to ''")
    data['Homepage'] = homepage
    repositories = [str(url) for url in package.urls if url.type == 'repository']
    repository = repositories[0] if repositories else ''
    data['Source'] = repository
    bugtrackers = [str(url) for url in package.urls if url.type == 'bugtracker']
    bugtracker = bugtrackers[0] if bugtrackers else ''
    data['BugTracker'] = bugtracker
    # Debian Increment Number
    data['DebianInc'] = '' if native else '-{0}'.format(deb_inc)
    # Debian Package Format
    data['format'] = 'native' if native else 'quilt'
    # Package name
    data['Package'] = sanitize_package_name(package.name)
    # Installation prefix
    data['InstallationPrefix'] = installation_prefix
    # Resolve dependencies
    evaluate_package_conditions(package, ros_distro)
    depends = [
        dep for dep in (package.run_depends + package.buildtool_export_depends)
        if dep.evaluated_condition is not False]
    build_depends = [
        dep for dep in (package.build_depends + package.buildtool_depends)
        if dep.evaluated_condition is not False]
    test_depends = [
        dep for dep in (package.test_depends)
        if dep.evaluated_condition is not False]
    replaces = [
        dep for dep in package.replaces
        if dep.evaluated_condition is not False]
    conflicts = [
        dep for dep in package.conflicts
        if dep.evaluated_condition is not False]
    unresolved_keys = depends + build_depends + test_depends + replaces + conflicts
    # The installer key is not considered here, but it is checked when the keys are checked before this
    resolved_deps = resolve_dependencies(unresolved_keys, os_name,
                                         os_version, ros_distro,
                                         peer_packages + [d.name for d in (replaces + conflicts)],
                                         fallback_resolver)
    data['Depends'] = sorted(
        set(format_depends(depends, resolved_deps))
    )
    # For more information on <!nocheck>, see
    # https://wiki.debian.org/BuildProfileSpec
    data['BuildDepends'] = sorted(
        set(format_depends(build_depends, resolved_deps)) |
        set(p + ' <!nocheck>' for p in format_depends(test_depends, resolved_deps))
    )
    data['Replaces'] = sorted(
        set(format_depends(replaces, resolved_deps))
    )
    data['Conflicts'] = sorted(
        set(format_depends(conflicts, resolved_deps))
    )

    # Build-type specific substitutions.
    build_type = package.get_build_type()
    if build_type == 'catkin':
        pass
    elif build_type == 'cmake':
        pass
    elif build_type == 'meson':
        pass
    elif build_type == 'ament_cmake':
        pass
    elif build_type == 'ament_python':
        # Don't set the install-scripts flag if it's already set in setup.cfg.
        package_path = os.path.abspath(os.path.dirname(package.filename))
        setup_cfg_path = os.path.join(package_path, 'setup.cfg')
        data['pass_install_scripts'] = True
        if os.path.isfile(setup_cfg_path):
            setup_cfg = ConfigParser()
            setup_cfg.read([setup_cfg_path])
            if (
                    setup_cfg.has_option('install', 'install-scripts') or
                    setup_cfg.has_option('install', 'install_scripts')
            ):
                data['pass_install_scripts'] = False
    else:
        error(
            "Build type '{}' is not supported by this version of bloom.".
            format(build_type), exit=True)

    # Set the distribution
    data['Distribution'] = os_version
    # Use the time stamp to set the date strings
    stamp = datetime.datetime.now(tz.tzlocal())
    data['Date'] = stamp.strftime('%a, %d %b %Y %T %z')
    data['YYYY'] = stamp.strftime('%Y')
    # Maintainers
    maintainers = []
    for m in package.maintainers:
        maintainers.append(str(m))
    data['Maintainer'] = maintainers[0]
    data['Maintainers'] = ', '.join(maintainers)
    # Changelog
    changelogs = get_changelogs(package, releaser_history)
    if changelogs and package.version not in [x[0] for x in changelogs]:
        warning("")
        warning("A CHANGELOG.rst was found, but no changelog for this version was found.")
        warning("You REALLY should have a entry (even a blank one) for each version of your package.")
        warning("")
    if not changelogs:
        # Ensure at least a minimal changelog
        changelogs = []
    if package.version not in [x[0] for x in changelogs]:
        changelogs.insert(0, (
            package.version,
            get_rfc_2822_date(datetime.datetime.now()),
            '  * Autogenerated, no changelog for this version found in CHANGELOG.rst.',
            package.maintainers[0].name,
            package.maintainers[0].email
        ))
    bad_changelog = False
    # Make sure that the first change log is the version being released
    if package.version != changelogs[0][0]:
        error("")
        error("The version of the first changelog entry '{0}' is not the "
              "same as the version being currently released '{1}'."
              .format(package.version, changelogs[0][0]))
        bad_changelog = True
    # Make sure that the current version is the latest in the changelog
    for changelog in changelogs:
        if parse_version(package.version) < parse_version(changelog[0]):
            error("")
            error("There is at least one changelog entry, '{0}', which has a "
                  "newer version than the version of package '{1}' being released, '{2}'."
                  .format(changelog[0], package.name, package.version))
            bad_changelog = True
    if bad_changelog:
        error("This is almost certainly by mistake, you should really take a "
              "look at the changelogs for the package you are releasing.")
        error("")
        if not maybe_continue('n', 'Continue anyways'):
            sys.exit("User quit.")
    data['changelogs'] = changelogs
    # Use debhelper version 7 for oneric, otherwise 9
    data['debhelper_version'] = 7 if os_version in ['oneiric'] else 9
    # Summarize dependencies
    summarize_dependency_mapping(data, depends, build_depends, resolved_deps)
    # Copyright
    licenses = []
    for l in package.licenses:
        if hasattr(l, 'file') and l.file is not None:
            license_file = os.path.join(os.path.dirname(package.filename), l.file)
            if not os.path.exists(license_file):
                error("License file '{}' is not found.".
                      format(license_file), exit=True)
            license_text = open(license_file, 'r').read().rstrip()
            licenses.append((str(l), format_multiline(license_text)))
        else:
            licenses.append((str(l), 'See repository for full license text'))
    data['Licenses'] = licenses

    def convertToUnicode(obj):
        if sys.version_info.major == 2:
            if isinstance(obj, str):
                return unicode(obj.decode('utf8'))
            elif isinstance(obj, unicode):
                return obj
        else:
            if isinstance(obj, bytes):
                return str(obj.decode('utf8'))
            elif isinstance(obj, str):
                return obj
        if isinstance(obj, list):
            for i, val in enumerate(obj):
                obj[i] = convertToUnicode(val)
            return obj
        elif isinstance(obj, type(None)):
            return None
        elif isinstance(obj, tuple):
            obj_tmp = list(obj)
            for i, val in enumerate(obj_tmp):
                obj_tmp[i] = convertToUnicode(obj_tmp[i])
            return tuple(obj_tmp)
        elif isinstance(obj, int):
            return obj
        raise RuntimeError('need to deal with type %s' % (str(type(obj))))

    for item in data.items():
        data[item[0]] = convertToUnicode(item[1])

    return data


def __process_template_folder(path, subs):
    items = os.listdir(path)
    processed_items = []
    for item in list(items):
        item = os.path.abspath(os.path.join(path, item))
        if os.path.basename(item) in ['.', '..', '.git', '.svn']:
            continue
        if os.path.isdir(item):
            sub_items = __process_template_folder(item, subs)
            processed_items.extend([os.path.join(item, s) for s in sub_items])
        if not item.endswith(TEMPLATE_EXTENSION):
            continue
        with open(item, 'r') as f:
            template = f.read()
        # Remove extension
        template_path = item[:-len(TEMPLATE_EXTENSION)]
        # Expand template
        info("Expanding '{0}' -> '{1}'".format(
            os.path.relpath(item),
            os.path.relpath(template_path)))
        result = em.expand(template, **subs)
        # Don't write an empty file
        if len(result) == 0 and \
           os.path.basename(template_path) in ['copyright']:
            processed_items.append(item)
            continue
        # Write the result
        with io.open(template_path, 'w', encoding='utf-8') as f:
            if sys.version_info.major == 2:
                result = result.decode('utf-8')
            f.write(result)
        # Copy the permissions
        shutil.copymode(item, template_path)
        processed_items.append(item)
    return processed_items


def process_template_files(path, subs):
    info(fmt("@!@{bf}==>@| In place processing templates in 'debian' folder."))
    debian_dir = os.path.join(path, 'debian')
    if not os.path.exists(debian_dir):
        sys.exit("No debian directory found at '{0}', cannot process templates."
                 .format(debian_dir))
    return __process_template_folder(debian_dir, subs)


def match_branches_with_prefix(prefix, get_branches, prune=False):
    debug("match_branches_with_prefix(" + str(prefix) + ", " +
          str(get_branches()) + ")")
    branches = []
    # Match branches
    existing_branches = get_branches()
    for branch in existing_branches:
        if branch.startswith('remotes/origin/'):
            branch = branch.split('/', 2)[-1]
        if branch.startswith(prefix):
            branches.append(branch)
    branches = list(set(branches))
    if prune:
        # Prune listed branches by packages in latest upstream
        with inbranch('upstream'):
            pkg_names, version, pkgs_dict = get_package_data('upstream')
            for branch in branches:
                if branch.split(prefix)[-1].strip('/') not in pkg_names:
                    branches.remove(branch)
    return branches


def get_package_from_branch(branch):
    with inbranch(branch):
        try:
            package_data = get_package_data(branch)
        except SystemExit:
            return None
        if type(package_data) not in [list, tuple]:
            # It is a ret code
            DebianGenerator.exit(package_data)
    names, version, packages = package_data
    if type(names) is list and len(names) > 1:
        DebianGenerator.exit(
            "Debian generator does not support generating "
            "from branches with multiple packages in them, use "
            "the release generator first to split packages into "
            "individual branches.")
    if type(packages) is dict:
        return list(packages.values())[0]


def debianize_string(value):
    markup_remover = re.compile(r'<.*?>')
    value = markup_remover.sub('', value)
    value = re.sub(r'\s+', ' ', value)
    value = value.strip()
    return value


def sanitize_package_name(name):
    return name.replace('_', '-')


class DebianGenerator(BloomGenerator):
    title = 'debian'
    description = "Generates debians from the catkin meta data"
    has_run_rosdep = os.environ.get('BLOOM_SKIP_ROSDEP_UPDATE', '0').lower() not in ['0', 'f', 'false', 'n', 'no']
    default_install_prefix = '/usr'
    rosdistro = os.environ.get('ROS_DISTRO', 'indigo')

    def prepare_arguments(self, parser):
        # Add command line arguments for this generator
        add = parser.add_argument
        add('-i', '--debian-inc', help="debian increment number", default='0')
        add('-p', '--prefix', required=True,
            help="branch prefix to match, and from which create debians"
                 " hint: if you want to match 'release/foo' use 'release'")
        add('-a', '--match-all', default=False, action="store_true",
            help="match all branches with the given prefix, "
                 "even if not in current upstream")
        add('--distros', nargs='+', required=False, default=[],
            help='A list of debian (ubuntu) distros to generate for')
        add('--install-prefix', default=None,
            help="overrides the default installation prefix (/usr)")
        add('--os-name', default='ubuntu',
            help="overrides os_name, set to 'ubuntu' by default")
        add('--os-not-required', default=False, action="store_true",
            help="Do not error if this os is not in the platforms "
                 "list for rosdistro")

    def handle_arguments(self, args):
        self.interactive = args.interactive
        self.debian_inc = args.debian_inc
        self.os_name = args.os_name
        self.distros = args.distros
        if self.distros in [None, []]:
            index = rosdistro.get_index(rosdistro.get_index_url())
            distribution_file = rosdistro.get_distribution_file(index, self.rosdistro)
            if self.os_name not in distribution_file.release_platforms:
                if args.os_not_required:
                    warning("No platforms defined for os '{0}' in release file for the "
                            "'{1}' distro. This os was not required; continuing without error."
                            .format(self.os_name, self.rosdistro))
                    sys.exit(0)
                error("No platforms defined for os '{0}' in release file for the '{1}' distro."
                      .format(self.os_name, self.rosdistro), exit=True)
            self.distros = distribution_file.release_platforms[self.os_name]
        self.install_prefix = args.install_prefix
        if args.install_prefix is None:
            self.install_prefix = self.default_install_prefix
        self.prefix = args.prefix
        self.branches = match_branches_with_prefix(self.prefix, get_branches, prune=not args.match_all)
        if len(self.branches) == 0:
            error(
                "No packages found, check your --prefix or --src arguments.",
                exit=True
            )
        self.packages = {}
        self.tag_names = {}
        self.names = []
        self.branch_args = []
        self.debian_branches = []
        for branch in self.branches:
            package = get_package_from_branch(branch)
            if package is None:
                # This is an ignored package
                continue
            self.packages[package.name] = package
            self.names.append(package.name)
            args = self.generate_branching_arguments(package, branch)
            # First branch is debian/[<rosdistro>/]<package>
            self.debian_branches.append(args[0][0])
            self.branch_args.extend(args)

    def summarize(self):
        info("Generating source debs for the packages: " + str(self.names))
        info("Debian Incremental Version: " + str(self.debian_inc))
        info("Debian Distributions: " + str(self.distros))

    def get_branching_arguments(self):
        return self.branch_args

    def update_rosdep(self):
        update_rosdep()
        self.has_run_rosdep = True

    def _check_all_keys_are_valid(self, peer_packages, ros_distro):
        keys_to_resolve = []
        key_to_packages_which_depends_on = collections.defaultdict(list)
        keys_to_ignore = set()
        for package in self.packages.values():
            evaluate_package_conditions(package, ros_distro)
            depends = [
                dep for dep in (package.run_depends + package.buildtool_export_depends)
                if dep.evaluated_condition is not False]
            build_depends = [
                dep for dep in (package.build_depends + package.buildtool_depends + package.test_depends)
                if dep.evaluated_condition is not False]
            unresolved_keys = [
                dep for dep in (depends + build_depends + package.replaces + package.conflicts)
                if dep.evaluated_condition is not False]
            keys_to_ignore = {
                    dep for dep in keys_to_ignore.union(package.replaces + package.conflicts)
                    if dep.evaluated_condition is not False}
            keys = [d.name for d in unresolved_keys]
            keys_to_resolve.extend(keys)
            for key in keys:
                key_to_packages_which_depends_on[key].append(package.name)

        os_name = self.os_name
        rosdistro = self.rosdistro
        all_keys_valid = True
        for key in sorted(set(keys_to_resolve)):
            for os_version in self.distros:
                try:
                    extended_peer_packages = peer_packages + [d.name for d in keys_to_ignore]
                    rule, installer_key, default_installer_key = \
                        resolve_rosdep_key(key, os_name, os_version, rosdistro, extended_peer_packages,
                                           retry=False)
                    if rule is None:
                        continue
                    if installer_key != default_installer_key:
                        error("Key '{0}' resolved to '{1}' with installer '{2}', "
                              "which does not match the default installer '{3}'."
                              .format(key, rule, installer_key, default_installer_key))
                        BloomGenerator.exit(
                            "The Debian generator does not support dependencies "
                            "which are installed with the '{0}' installer."
                            .format(installer_key),
                            returncode=code.GENERATOR_INVALID_INSTALLER_KEY)
                except (GeneratorError, RuntimeError) as e:
                    print(fmt("Failed to resolve @{cf}@!{key}@| on @{bf}{os_name}@|:@{cf}@!{os_version}@| with: {e}")
                          .format(**locals()))
                    print(fmt("@{cf}@!{0}@| is depended on by these packages: ").format(key) +
                          str(list(set(key_to_packages_which_depends_on[key]))))
                    print(fmt("@{kf}@!<== @{rf}@!Failed@|"))
                    all_keys_valid = False
        return all_keys_valid

    def pre_modify(self):
        info("\nPre-verifying Debian dependency keys...")
        # Run rosdep update is needed
        if not self.has_run_rosdep:
            self.update_rosdep()

        peer_packages = [p.name for p in self.packages.values()]

        while not self._check_all_keys_are_valid(peer_packages, self.rosdistro):
            error("Some of the dependencies for packages in this repository could not be resolved by rosdep.")
            if not self.interactive:
                sys.exit(code.GENERATOR_NO_ROSDEP_KEY_FOR_DISTRO)
            error("You can try to address the issues which appear above and try again if you wish.")
            try:
                if not maybe_continue(msg="Would you like to try again?"):
                    error("User aborted after rosdep keys were not resolved.")
                    sys.exit(code.GENERATOR_NO_ROSDEP_KEY_FOR_DISTRO)
            except (KeyboardInterrupt, EOFError):
                error("\nUser quit.", exit=True)
            update_rosdep()
            invalidate_view_cache()

        info("All keys are " + ansi('greenf') + "OK" + ansi('reset') + "\n")

    def pre_branch(self, destination, source):
        if destination in self.debian_branches:
            return
        # Run rosdep update is needed
        if not self.has_run_rosdep:
            self.update_rosdep()
        # Determine the current package being generated
        name = destination.split('/')[-1]
        distro = destination.split('/')[-2]
        # Retrieve the package
        package = self.packages[name]
        # Report on this package
        self.summarize_package(package, distro)

    def pre_rebase(self, destination):
        # Get the stored configs is any
        patches_branch = 'patches/' + destination
        config = self.load_original_config(patches_branch)
        if config is not None:
            curr_config = get_patch_config(patches_branch)
            if curr_config['parent'] == config['parent']:
                set_patch_config(patches_branch, config)

    def post_rebase(self, destination):
        name = destination.split('/')[-1]
        # Retrieve the package
        package = self.packages[name]
        # Handle differently if this is a debian vs distro branch
        if destination in self.debian_branches:
            info("Placing debian template files into '{0}' branch."
                 .format(destination))
            # Then this is a debian branch
            # Place the raw template files
            self.place_template_files(package.get_build_type())
        else:
            # This is a distro specific debian branch
            # Determine the current package being generated
            distro = destination.split('/')[-2]
            # Create debians for each distro
            with inbranch(destination):
                data = self.generate_debian(package, distro)
                # Create the tag name for later
                self.tag_names[destination] = self.generate_tag_name(data)
        # Update the patch configs
        patches_branch = 'patches/' + destination
        config = get_patch_config(patches_branch)
        # Store it
        self.store_original_config(config, patches_branch)
        # Modify the base so import/export patch works
        current_branch = get_current_branch()
        if current_branch is None:
            error("Could not determine current branch.", exit=True)
        config['base'] = get_commit_hash(current_branch)
        # Set it
        set_patch_config(patches_branch, config)

    def post_patch(self, destination, color='bluef'):
        if destination in self.debian_branches:
            return
        # Tag after patches have been applied
        with inbranch(destination):
            # Tag
            tag_name = self.tag_names[destination]
            if tag_exists(tag_name):
                if self.interactive:
                    warning("Tag exists: " + tag_name)
                    warning("Do you wish to overwrite it?")
                    if not maybe_continue('y'):
                        error("Answered no to continue, aborting.", exit=True)
                else:
                    warning("Overwriting tag: " + tag_name)
            else:
                info("Creating tag: " + tag_name)
            execute_command('git tag -f ' + tag_name)
        # Report of success
        name = destination.split('/')[-1]
        package = self.packages[name]
        distro = destination.split('/')[-2]
        info(ansi(color) + "####" + ansi('reset'), use_prefix=False)
        info(
            ansi(color) + "#### " + ansi('greenf') + "Successfully" +
            ansi(color) + " generated '" + ansi('boldon') + distro +
            ansi('boldoff') + "' debian for package"
            " '" + ansi('boldon') + package.name + ansi('boldoff') + "'" +
            " at version '" + ansi('boldon') + package.version +
            "-" + str(self.debian_inc) + ansi('boldoff') + "'" +
            ansi('reset'),
            use_prefix=False
        )
        info(ansi(color) + "####\n" + ansi('reset'), use_prefix=False)

    def store_original_config(self, config, patches_branch):
        with inbranch(patches_branch):
            with open('debian.store', 'w+') as f:
                f.write(json.dumps(config))
            execute_command('git add debian.store')
            if has_changes():
                execute_command('git commit -m "Store original patch config"')

    def load_original_config(self, patches_branch):
        config_store = show(patches_branch, 'debian.store')
        if config_store is None:
            return config_store
        return json.loads(config_store)

    def place_template_files(self, build_type, debian_dir='debian'):
        # Create/Clean the debian folder
        if os.path.exists(debian_dir):
            if self.interactive:
                warning("debian directory exists: " + debian_dir)
                warning("Do you wish to overwrite it?")
                if not maybe_continue('y'):
                    error("Answered no to continue, aborting.", exit=True)
            elif 'BLOOM_CLEAR_DEBIAN_ON_GENERATION' in os.environ:
                warning("Overwriting debian directory: " + debian_dir)
                execute_command('git rm -rf ' + debian_dir)
                execute_command('git commit -m "Clearing previous debian folder"')
                if os.path.exists(debian_dir):
                    shutil.rmtree(debian_dir)
            else:
                warning("Not overwriting debian directory.")
        # Use generic place template files command
        place_template_files('.', build_type, gbp=True)
        # Commit results
        execute_command('git add ' + debian_dir)
        _, has_files, _ = execute_command('git diff --cached --name-only', return_io=True)
        if has_files:
            execute_command('git commit -m "Placing debian template files"')

    def get_releaser_history(self):
        # Assumes that this is called in the target branch
        patches_branch = 'patches/' + get_current_branch()
        raw = show(patches_branch, 'releaser_history.json')
        return None if raw is None else json.loads(raw)

    def set_releaser_history(self, history):
        # Assumes that this is called in the target branch
        patches_branch = 'patches/' + get_current_branch()
        debug("Writing release history to '{0}' branch".format(patches_branch))
        with inbranch(patches_branch):
            with open('releaser_history.json', 'w') as f:
                f.write(json.dumps(history))
            execute_command('git add releaser_history.json')
            if has_changes():
                execute_command('git commit -m "Store releaser history"')

    def get_subs(self, package, debian_distro, releaser_history=None):
        return generate_substitutions_from_package(
            package,
            self.os_name,
            debian_distro,
            self.rosdistro,
            self.install_prefix,
            self.debian_inc,
            [p.name for p in self.packages.values()],
            releaser_history=releaser_history,
            fallback_resolver=missing_dep_resolver
        )

    def generate_debian(self, package, debian_distro):
        info("Generating debian for {0}...".format(debian_distro))
        # Try to retrieve the releaser_history
        releaser_history = self.get_releaser_history()
        # Generate substitution values
        subs = self.get_subs(package, debian_distro, releaser_history)
        # Use subs to create and store releaser history
        releaser_history = [(v, (n, e)) for v, _, _, n, e in subs['changelogs']]
        self.set_releaser_history(dict(releaser_history))
        # Handle gbp.conf
        subs['release_tag'] = self.get_release_tag(subs)
        # Template files
        template_files = process_template_files('.', subs)
        # Remove any residual template files
        execute_command('git rm -rf ' + ' '.join("'{}'".format(t) for t in template_files))
        # Add changes to the debian folder
        execute_command('git add debian')
        # Commit changes
        execute_command('git commit -m "Generated debian files for ' +
                        debian_distro + '"')
        # Return the subs for other use
        return subs

    def get_release_tag(self, data):
        return 'release/{0}/{1}-{2}'.format(data['Name'], data['Version'],
                                            self.debian_inc)

    def generate_tag_name(self, data):
        tag_name = '{Package}_{Version}{DebianInc}_{Distribution}'
        tag_name = 'debian/' + tag_name.format(**data)
        return tag_name

    def generate_branching_arguments(self, package, branch):
        n = package.name
        # Debian branch
        deb_branch = 'debian/' + n
        # Branch first to the debian branch
        args = [[deb_branch, branch, False]]
        # Then for each debian distro, branch from the base debian branch
        args.extend([
            ['debian/' + d + '/' + n, deb_branch, False] for d in self.distros
        ])
        return args

    def summarize_package(self, package, distro, color='bluef'):
        info(ansi(color) + "\n####" + ansi('reset'), use_prefix=False)
        info(
            ansi(color) + "#### Generating '" + ansi('boldon') + distro +
            ansi('boldoff') + "' debian for package"
            " '" + ansi('boldon') + package.name + ansi('boldoff') + "'" +
            " at version '" + ansi('boldon') + package.version +
            "-" + str(self.debian_inc) + ansi('boldoff') + "'" +
            ansi('reset'),
            use_prefix=False
        )
        info(ansi(color) + "####" + ansi('reset'), use_prefix=False)
