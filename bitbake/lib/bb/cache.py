# ex:ts=4:sw=4:sts=4:et
# -*- tab-width: 4; c-basic-offset: 4; indent-tabs-mode: nil -*-
#
# BitBake 'Event' implementation
#
# Caching of bitbake variables before task execution

# Copyright (C) 2006        Richard Purdie

# but small sections based on code from bin/bitbake:
# Copyright (C) 2003, 2004  Chris Larson
# Copyright (C) 2003, 2004  Phil Blundell
# Copyright (C) 2003 - 2005 Michael 'Mickey' Lauer
# Copyright (C) 2005        Holger Hans Peter Freyther
# Copyright (C) 2005        ROAD GmbH
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.


import os
import logging
from collections import defaultdict, namedtuple
import bb.data
import bb.utils

logger = logging.getLogger("BitBake.Cache")

try:
    import cPickle as pickle
except ImportError:
    import pickle
    logger.info("Importing cPickle failed. "
                "Falling back to a very slow implementation.")

__cache_version__ = "132"

recipe_fields = (
    'pn',
    'pv',
    'pr',
    'pe',
    'defaultpref',
    'depends',
    'provides',
    'task_deps',
    'stamp',
    'broken',
    'not_world',
    'skipped',
    'timestamp',
    'packages',
    'packages_dynamic',
    'rdepends',
    'rdepends_pkg',
    'rprovides',
    'rprovides_pkg',
    'rrecommends',
    'rrecommends_pkg',
    'nocache',
    'variants',
    'file_depends',
)


class RecipeInfo(namedtuple('RecipeInfo', recipe_fields)):
    __slots__ = ()

    @classmethod
    def listvar(cls, var, metadata):
        return cls.getvar(var, metadata).split()

    @classmethod
    def intvar(cls, var, metadata):
        return int(cls.getvar(var, metadata) or 0)

    @classmethod
    def depvar(cls, var, metadata):
        return bb.utils.explode_deps(cls.getvar(var, metadata))

    @classmethod
    def pkgvar(cls, var, packages, metadata):
        return dict((pkg, cls.depvar("%s_%s" % (var, pkg), metadata))
                    for pkg in packages)

    @classmethod
    def getvar(cls, var, metadata):
        return metadata.getVar(var, True) or ''

    @classmethod
    def from_metadata(cls, filename, metadata):
        packages = cls.listvar('PACKAGES', metadata)
        return RecipeInfo(
            file_depends     = metadata.getVar('__depends', False),
            task_deps        = metadata.getVar('_task_deps', False) or
                               {'tasks': [], 'parents': {}},
            variants         = cls.listvar('__VARIANTS', metadata) + [''],
            skipped          = cls.getvar('__SKIPPED', metadata),
            timestamp        = bb.parse.cached_mtime(filename),
            packages         = packages,
            pn               = cls.getvar('PN', metadata),
            pe               = cls.getvar('PE', metadata),
            pv               = cls.getvar('PV', metadata),
            pr               = cls.getvar('PR', metadata),
            nocache          = cls.getvar('__BB_DONT_CACHE', metadata),
            defaultpref      = cls.intvar('DEFAULT_PREFERENCE', metadata),
            broken           = cls.getvar('BROKEN', metadata),
            not_world        = cls.getvar('EXCLUDE_FROM_WORLD', metadata),
            stamp            = cls.getvar('STAMP', metadata),
            packages_dynamic = cls.listvar('PACKAGES_DYNAMIC', metadata),
            depends          = cls.depvar('DEPENDS', metadata),
            provides         = cls.depvar('PROVIDES', metadata),
            rdepends         = cls.depvar('RDEPENDS', metadata),
            rprovides        = cls.depvar('RPROVIDES', metadata),
            rrecommends      = cls.depvar('RRECOMMENDS', metadata),
            rprovides_pkg    = cls.pkgvar('RPROVIDES', packages, metadata),
            rdepends_pkg     = cls.pkgvar('RDEPENDS', packages, metadata),
            rrecommends_pkg  = cls.pkgvar('RRECOMMENDS', packages, metadata),
        )


class Cache(object):
    """
    BitBake Cache implementation
    """

    def __init__(self, data):
        self.cachedir = bb.data.getVar("CACHE", data, True)
        self.clean = set()
        self.checked = set()
        self.depends_cache = {}
        self.data = None
        self.data_fn = None
        self.cacheclean = True

        if self.cachedir in [None, '']:
            self.has_cache = False
            logger.info("Not using a cache. "
                        "Set CACHE = <directory> to enable.")
            return

        self.has_cache = True
        self.cachefile = os.path.join(self.cachedir, "bb_cache.dat")

        logger.debug(1, "Using cache in '%s'", self.cachedir)
        bb.utils.mkdirhier(self.cachedir)

        # If any of configuration.data's dependencies are newer than the
        # cache there isn't even any point in loading it...
        newest_mtime = 0
        deps = bb.data.getVar("__depends", data)

        old_mtimes = [old_mtime for _, old_mtime in deps]
        old_mtimes.append(newest_mtime)
        newest_mtime = max(old_mtimes)

        if bb.parse.cached_mtime_noerror(self.cachefile) >= newest_mtime:
            try:
                p = pickle.Unpickler(file(self.cachefile, "rb"))
                self.depends_cache, version_data = p.load()
                if version_data['CACHE_VER'] != __cache_version__:
                    raise ValueError('Cache Version Mismatch')
                if version_data['BITBAKE_VER'] != bb.__version__:
                    raise ValueError('Bitbake Version Mismatch')
            except EOFError:
                logger.info("Truncated cache found, rebuilding...")
                self.depends_cache = {}
            except:
                logger.info("Invalid cache found, rebuilding...")
                self.depends_cache = {}
        else:
            if os.path.isfile(self.cachefile):
                logger.info("Out of date cache found, rebuilding...")

    @staticmethod
    def virtualfn2realfn(virtualfn):
        """
        Convert a virtual file name to a real one + the associated subclass keyword
        """

        fn = virtualfn
        cls = ""
        if virtualfn.startswith('virtual:'):
            cls = virtualfn.split(':', 2)[1]
            fn = virtualfn.replace('virtual:' + cls + ':', '')
        return (fn, cls)

    @staticmethod
    def realfn2virtual(realfn, cls):
        """
        Convert a real filename + the associated subclass keyword to a virtual filename
        """
        if cls == "":
            return realfn
        return "virtual:" + cls + ":" + realfn

    @classmethod
    def loadDataFull(cls, virtualfn, appends, cfgData):
        """
        Return a complete set of data for fn.
        To do this, we need to parse the file.
        """

        (fn, virtual) = cls.virtualfn2realfn(virtualfn)

        logger.debug(1, "Parsing %s (full)", fn)

        bb_data = cls.load_bbfile(fn, appends, cfgData)
        return bb_data[virtual]

    def loadData(self, fn, appends, cfgData, cacheData):
        """
        Load a subset of data for fn.
        If the cached data is valid we do nothing,
        To do this, we need to parse the file and set the system
        to record the variables accessed.
        Return the cache status and whether the file was skipped when parsed
        """
        skipped, virtuals = 0, 0

        if fn not in self.checked:
            self.cacheValidUpdate(fn)

        cached = self.cacheValid(fn)
        if not cached:
            logger.debug(1, "Parsing %s", fn)
            datastores = self.load_bbfile(fn, appends, cfgData)
            depends = set()
            for variant, data in sorted(datastores.iteritems(),
                                        key=lambda i: i[0],
                                        reverse=True):
                virtualfn = self.realfn2virtual(fn, variant)
                depends |= (data.getVar("__depends", False) or set())
                if depends and not variant:
                    data.setVar("__depends", depends)
                info = RecipeInfo.from_metadata(fn, data)
                if not info.nocache:
                    # The recipe was parsed, and is not marked as being
                    # uncacheable, so we need to ensure that we write out the
                    # new cache data.
                    self.cacheclean = False
                self.depends_cache[virtualfn] = info

        info = self.depends_cache[fn]
        for variant in info.variants:
            virtualfn = self.realfn2virtual(fn, variant)
            vinfo = self.depends_cache[virtualfn]
            if vinfo.skipped:
                logger.debug(1, "Skipping %s", virtualfn)
                skipped += 1
            else:
                cacheData.add_from_recipeinfo(virtualfn, vinfo)
                virtuals += 1

        return cached, skipped, virtuals

    def cacheValid(self, fn):
        """
        Is the cache valid for fn?
        Fast version, no timestamps checked.
        """
        # Is cache enabled?
        if not self.has_cache:
            return False
        if fn in self.clean:
            return True
        return False

    def cacheValidUpdate(self, fn):
        """
        Is the cache valid for fn?
        Make thorough (slower) checks including timestamps.
        """
        # Is cache enabled?
        if not self.has_cache:
            return False

        self.checked.add(fn)

        # Pretend we're clean so getVar works
        self.clean.add(fn)

        # File isn't in depends_cache
        if not fn in self.depends_cache:
            logger.debug(2, "Cache: %s is not cached", fn)
            self.remove(fn)
            return False

        mtime = bb.parse.cached_mtime_noerror(fn)

        # Check file still exists
        if mtime == 0:
            logger.debug(2, "Cache: %s no longer exists", fn)
            self.remove(fn)
            return False

        info = self.depends_cache[fn]
        # Check the file's timestamp
        if mtime != info.timestamp:
            logger.debug(2, "Cache: %s changed", fn)
            self.remove(fn)
            return False

        # Check dependencies are still valid
        depends = info.file_depends
        if depends:
            for f, old_mtime in depends:
                fmtime = bb.parse.cached_mtime_noerror(f)
                # Check if file still exists
                if old_mtime != 0 and fmtime == 0:
                    logger.debug(2, "Cache: %s's dependency %s was removed",
                                    fn, f)
                    self.remove(fn)
                    return False

                if (fmtime != old_mtime):
                    logger.debug(2, "Cache: %s's dependency %s changed",
                                    fn, f)
                    self.remove(fn)
                    return False

        invalid = False
        for cls in info.variants:
            virtualfn = self.realfn2virtual(fn, cls)
            self.clean.add(virtualfn)
            if virtualfn not in self.depends_cache:
                logger.debug(2, "Cache: %s is not cached", virtualfn)
                invalid = True

        # If any one of the variants is not present, mark as invalid for all
        if invalid:
            for cls in info.variants:
                virtualfn = self.realfn2virtual(fn, cls)
                if virtualfn in self.clean:
                    logger.debug(2, "Cache: Removing %s from cache", virtualfn)
                    self.clean.remove(virtualfn)
            if fn in self.clean:
                logger.debug(2, "Cache: Marking %s as not clean", fn)
                self.clean.remove(fn)
            return False

        return True

    def remove(self, fn):
        """
        Remove a fn from the cache
        Called from the parser in error cases
        """
        if fn in self.depends_cache:
            logger.debug(1, "Removing %s from cache", fn)
            del self.depends_cache[fn]
        if fn in self.clean:
            logger.debug(1, "Marking %s as unclean", fn)
            self.clean.remove(fn)

    def sync(self):
        """
        Save the cache
        Called from the parser when complete (or exiting)
        """

        if not self.has_cache:
            return

        if self.cacheclean:
            logger.debug(2, "Cache is clean, not saving.")
            return

        version_data = {}
        version_data['CACHE_VER'] = __cache_version__
        version_data['BITBAKE_VER'] = bb.__version__

        cache_data = dict(self.depends_cache)
        for fn, info in self.depends_cache.iteritems():
            if info.nocache:
                logger.debug(2, "Not caching %s, marked as not cacheable", fn)
                del cache_data[fn]
            elif info.pv and 'SRCREVINACTION' in info.pv:
                logger.error("Not caching %s as it had SRCREVINACTION in PV. "
                             "Please report this bug", fn)
                del cache_data[fn]

        p = pickle.Pickler(file(self.cachefile, "wb"), -1)
        p.dump([cache_data, version_data])
        del self.depends_cache

    @staticmethod
    def mtime(cachefile):
        return bb.parse.cached_mtime_noerror(cachefile)

    def add(self, file_name, data, cacheData):
        """
        Save data we need into the cache
        """

        realfn = self.virtualfn2realfn(file_name)[0]
        info = RecipeInfo.from_metadata(realfn, data)
        self.depends_cache[file_name] = info
        cacheData.add_from_recipeinfo(file_name, info)

    @staticmethod
    def load_bbfile(bbfile, appends, config):
        """
        Load and parse one .bb build file
        Return the data and whether parsing resulted in the file being skipped
        """
        chdir_back = False

        from bb import data, parse

        # expand tmpdir to include this topdir
        data.setVar('TMPDIR', data.getVar('TMPDIR', config, 1) or "", config)
        bbfile_loc = os.path.abspath(os.path.dirname(bbfile))
        oldpath = os.path.abspath(os.getcwd())
        parse.cached_mtime_noerror(bbfile_loc)
        bb_data = data.init_db(config)
        # The ConfHandler first looks if there is a TOPDIR and if not
        # then it would call getcwd().
        # Previously, we chdir()ed to bbfile_loc, called the handler
        # and finally chdir()ed back, a couple of thousand times. We now
        # just fill in TOPDIR to point to bbfile_loc if there is no TOPDIR yet.
        if not data.getVar('TOPDIR', bb_data):
            chdir_back = True
            data.setVar('TOPDIR', bbfile_loc, bb_data)
        try:
            if appends:
                data.setVar('__BBAPPEND', " ".join(appends), bb_data)
            bb_data = parse.handle(bbfile, bb_data)
            if chdir_back:
                os.chdir(oldpath)
            return bb_data
        except:
            if chdir_back:
                os.chdir(oldpath)
            raise


def init(cooker):
    """
    The Objective: Cache the minimum amount of data possible yet get to the
    stage of building packages (i.e. tryBuild) without reparsing any .bb files.

    To do this, we intercept getVar calls and only cache the variables we see
    being accessed. We rely on the cache getVar calls being made for all
    variables bitbake might need to use to reach this stage. For each cached
    file we need to track:

    * Its mtime
    * The mtimes of all its dependencies
    * Whether it caused a parse.SkipPackage exception

    Files causing parsing errors are evicted from the cache.

    """
    return Cache(cooker.configuration.data)


class CacheData(object):
    """
    The data structures we compile from the cached data
    """

    def __init__(self):
        """
        Direct cache variables
        """
        self.providers = defaultdict(list)
        self.rproviders = defaultdict(list)
        self.packages = defaultdict(list)
        self.packages_dynamic = defaultdict(list)
        self.possible_world = []
        self.pkg_pn = defaultdict(list)
        self.pkg_fn = {}
        self.pkg_pepvpr = {}
        self.pkg_dp = {}
        self.pn_provides = defaultdict(list)
        self.fn_provides = {}
        self.all_depends = []
        self.deps = defaultdict(list)
        self.rundeps = defaultdict(lambda: defaultdict(list))
        self.runrecs = defaultdict(lambda: defaultdict(list))
        self.task_queues = {}
        self.task_deps = {}
        self.stamp = {}
        self.preferred = {}

        """
        Indirect Cache variables
        (set elsewhere)
        """
        self.ignored_dependencies = []
        self.world_target = set()
        self.bbfile_priority = {}
        self.bbfile_config_priorities = []

    def add_from_recipeinfo(self, fn, info):
        self.task_deps[fn] = info.task_deps
        self.pkg_fn[fn] = info.pn
        self.pkg_pn[info.pn].append(fn)
        self.pkg_pepvpr[fn] = (info.pe, info.pv, info.pr)
        self.pkg_dp[fn] = info.defaultpref
        self.stamp[fn] = info.stamp

        provides = [info.pn]
        for provide in info.provides:
            if provide not in provides:
                provides.append(provide)
        self.fn_provides[fn] = provides

        for provide in provides:
            self.providers[provide].append(fn)
            if provide not in self.pn_provides[info.pn]:
                self.pn_provides[info.pn].append(provide)

        for dep in info.depends:
            if dep not in self.deps[fn]:
                self.deps[fn].append(dep)
            if dep not in self.all_depends:
                self.all_depends.append(dep)

        rprovides = info.rprovides
        for package in info.packages:
            self.packages[package].append(fn)
            rprovides += info.rprovides_pkg[package]

        for package in info.packages_dynamic:
            self.packages_dynamic[package].append(fn)

        for rprovide in rprovides:
            self.rproviders[rprovide].append(fn)

        # Build hash of runtime depends and rececommends
        for package in info.packages + [info.pn]:
            rundeps, runrecs = list(info.rdepends), list(info.rrecommends)
            if package in info.packages:
                rundeps += info.rdepends_pkg[package]
                runrecs += info.rrecommends_pkg[package]
            self.rundeps[fn][package] = rundeps
            self.runrecs[fn][package] = runrecs

        # Collect files we may need for possible world-dep
        # calculations
        if not info.broken and not info.not_world:
            self.possible_world.append(fn)
