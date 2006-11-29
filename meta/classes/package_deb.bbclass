inherit package
DEPENDS_prepend="${@["dpkg-native ", ""][(bb.data.getVar('PACKAGES', d, 1) == '')]}"
BOOTSTRAP_EXTRA_RDEPENDS += "dpkg"
DISTRO_EXTRA_RDEPENDS += "dpkg"
PACKAGE_WRITE_FUNCS += "do_package_deb"
IMAGE_PKGTYPE ?= "deb"

python package_deb_fn () {
    from bb import data
    bb.data.setVar('PKGFN', bb.data.getVar('PKG',d), d)
}

addtask package_deb_install
python do_package_deb_install () {
    import os, sys
    pkg = bb.data.getVar('PKG', d, 1)
    pkgfn = bb.data.getVar('PKGFN', d, 1)
    rootfs = bb.data.getVar('IMAGE_ROOTFS', d, 1)
    debdir = bb.data.getVar('DEPLOY_DIR_DEB', d, 1)
    stagingdir = bb.data.getVar('STAGING_DIR', d, 1)
    stagingbindir = bb.data.getVar('STAGING_BINDIR_NATIVE', d, 1)
    tmpdir = bb.data.getVar('TMPDIR', d, 1)

    if None in (pkg,pkgfn,rootfs):
        raise bb.build.FuncFailed("missing variables (one or more of PKG, PKGFN, IMAGE_ROOTFS)")
    try:
        if not os.exists(rootfs):
            os.makedirs(rootfs)
        os.chdir(rootfs)
    except OSError:
        raise bb.build.FuncFailed(str(sys.exc_value))

    # update packages file
    (exitstatus, output) = commands.getstatusoutput('dpkg-scanpackages %s > %s/Packages' % (debdir, debdir))
    if (exitstatus != 0 ):
        raise bb.build.FuncFailed(output)

    f = open(os.path.join(tmpdir, "stamps", "do_packages"), "w")
    f.close()

    # NOTE: this env stuff is racy at best, we need something more capable
    # than 'commands' for command execution, which includes manipulating the
    # env of the fork+execve'd processs

    # Set up environment
    apt_config = os.getenv('APT_CONFIG')
    os.putenv('APT_CONFIG', os.path.join(stagingdir, 'etc', 'apt', 'apt.conf'))
    path = os.getenv('PATH')
    os.putenv('PATH', '%s:%s' % (stagingbindir, os.getenv('PATH')))

    # install package
    commands.getstatusoutput('apt-get update')
    commands.getstatusoutput('apt-get install -y %s' % pkgfn)

    # revert environment
    os.putenv('APT_CONFIG', apt_config)
    os.putenv('PATH', path)
}

python do_package_deb () {
    import copy # to back up env data
    import sys
    import re

    workdir = bb.data.getVar('WORKDIR', d, 1)
    if not workdir:
        bb.error("WORKDIR not defined, unable to package")
        return

    import os # path manipulations
    outdir = bb.data.getVar('DEPLOY_DIR_DEB', d, 1)
    if not outdir:
        bb.error("DEPLOY_DIR_DEB not defined, unable to package")
        return

    dvar = bb.data.getVar('D', d, 1)
    if not dvar:
        bb.error("D not defined, unable to package")
        return
    bb.mkdirhier(dvar)

    packages = bb.data.getVar('PACKAGES', d, 1)
    if not packages:
        bb.debug(1, "PACKAGES not defined, nothing to package")
        return

    tmpdir = bb.data.getVar('TMPDIR', d, 1)
    # Invalidate the packages file
    if os.access(os.path.join(tmpdir, "stamps", "do_packages"),os.R_OK):
        os.unlink(os.path.join(tmpdir, "stamps", "do_packages"))

    if packages == []:
        bb.debug(1, "No packages; nothing to do")
        return

    for pkg in packages.split():
        localdata = bb.data.createCopy(d)
        root = "%s/install/%s" % (workdir, pkg)

        bb.data.setVar('ROOT', '', localdata)
        bb.data.setVar('ROOT_%s' % pkg, root, localdata)
        pkgname = bb.data.getVar('PKG_%s' % pkg, localdata, 1)
        if not pkgname:
            pkgname = pkg
        bb.data.setVar('PKG', pkgname, localdata)

        overrides = bb.data.getVar('OVERRIDES', localdata)
        if not overrides:
            raise bb.build.FuncFailed('OVERRIDES not defined')
        overrides = bb.data.expand(overrides, localdata)
        bb.data.setVar('OVERRIDES', overrides + ':' + pkg, localdata)

        bb.data.update_data(localdata)
        basedir = os.path.join(os.path.dirname(root))

        pkgoutdir = os.path.join(outdir, bb.data.getVar('PACKAGE_ARCH', localdata, 1))
        bb.mkdirhier(pkgoutdir)

        os.chdir(root)
        from glob import glob
        g = glob('*')
        try:
            del g[g.index('DEBIAN')]
            del g[g.index('./DEBIAN')]
        except ValueError:
            pass
        if not g and not bb.data.getVar('ALLOW_EMPTY', localdata):
            from bb import note
            note("Not creating empty archive for %s-%s-%s" % (pkg, bb.data.getVar('PV', localdata, 1), bb.data.getVar('PR', localdata, 1)))
            continue
        controldir = os.path.join(root, 'DEBIAN')
        bb.mkdirhier(controldir)
        try:
            ctrlfile = file(os.path.join(controldir, 'control'), 'wb')
            # import codecs
            # ctrlfile = codecs.open("someFile", "w", "utf-8")
        except OSError:
            raise bb.build.FuncFailed("unable to open control file for writing.")

        fields = []
        fields.append(["Version: %s-%s\n", ['PV', 'PR']])
        fields.append(["Description: %s\n", ['DESCRIPTION']])
        fields.append(["Section: %s\n", ['SECTION']])
        fields.append(["Priority: %s\n", ['PRIORITY']])
        fields.append(["Maintainer: %s\n", ['MAINTAINER']])
        fields.append(["Architecture: %s\n", ['TARGET_ARCH']])
        fields.append(["OE: %s\n", ['P']])
        fields.append(["Homepage: %s\n", ['HOMEPAGE']])

#        Package, Version, Maintainer, Description - mandatory
#        Section, Priority, Essential, Architecture, Source, Depends, Pre-Depends, Recommends, Suggests, Conflicts, Replaces, Provides - Optional


        def pullData(l, d):
            l2 = []
            for i in l:
                data = bb.data.getVar(i, d, 1)
                if data is None:
                    raise KeyError(f)
		if i == 'TARGET_ARCH' and bb.data.getVar('PACKAGE_ARCH', d, 1) == 'all':
                    data = 'all'
                l2.append(data)
            return l2

        ctrlfile.write("Package: %s\n" % pkgname)
        # check for required fields
        try:
            for (c, fs) in fields:
                ctrlfile.write(unicode(c % tuple(pullData(fs, localdata))))
        except KeyError:
            (type, value, traceback) = sys.exc_info()
            ctrlfile.close()
            raise bb.build.FuncFailed("Missing field for deb generation: %s" % value)
        # more fields

        bb.build.exec_func("mapping_rename_hook", localdata)

        rdepends = explode_deps(unicode(bb.data.getVar("RDEPENDS", localdata, 1) or ""))
        rdepends = [dep for dep in rdepends if not '*' in dep]
        rrecommends = explode_deps(unicode(bb.data.getVar("RRECOMMENDS", localdata, 1) or ""))
        rrecommends = [rec for rec in rrecommends if not '*' in rec]
        rsuggests = (unicode(bb.data.getVar("RSUGGESTS", localdata, 1) or "")).split()
        rprovides = (unicode(bb.data.getVar("RPROVIDES", localdata, 1) or "")).split()
        rreplaces = (unicode(bb.data.getVar("RREPLACES", localdata, 1) or "")).split()
        rconflicts = (unicode(bb.data.getVar("RCONFLICTS", localdata, 1) or "")).split()
        if rdepends:
            ctrlfile.write(u"Depends: %s\n" % ", ".join(rdepends))
        if rsuggests:
            ctrlfile.write(u"Suggests: %s\n" % ", ".join(rsuggests))
        if rrecommends:
            ctrlfile.write(u"Recommends: %s\n" % ", ".join(rrecommends))
        if rprovides:
            ctrlfile.write(u"Provides: %s\n" % ", ".join(rprovides))
        if rreplaces:
            ctrlfile.write(u"Replaces: %s\n" % ", ".join(rreplaces))
        if rconflicts:
            ctrlfile.write(u"Conflicts: %s\n" % ", ".join(rconflicts))
        ctrlfile.close()

        for script in ["preinst", "postinst", "prerm", "postrm"]:
            scriptvar = bb.data.getVar('pkg_%s' % script, localdata, 1)
            if not scriptvar:
                continue
            try:
                scriptfile = file(os.path.join(controldir, script), 'w')
            except OSError:
                raise bb.build.FuncFailed("unable to open %s script file for writing." % script)
            scriptfile.write(scriptvar)
            scriptfile.close()
            os.chmod(os.path.join(controldir, script), 0755)

        conffiles_str = bb.data.getVar("CONFFILES", localdata, 1)
        if conffiles_str:
            try:
                conffiles = file(os.path.join(controldir, 'conffiles'), 'w')
            except OSError:
                raise bb.build.FuncFailed("unable to open conffiles for writing.")
            for f in conffiles_str.split():
                conffiles.write('%s\n' % f)
            conffiles.close()

        os.chdir(basedir)
        ret = os.system("PATH=\"%s\" fakeroot dpkg-deb -b %s %s" % (bb.data.getVar("PATH", localdata, 1), root, pkgoutdir))
        if ret != 0:
            raise bb.build.FuncFailed("dpkg-deb execution failed")

        for script in ["preinst", "postinst", "prerm", "postrm", "control" ]:
            scriptfile = os.path.join(controldir, script)
            try:
                os.remove(scriptfile)
            except OSError:
                pass
        try:
            os.rmdir(controldir)
        except OSError:
            pass
        del localdata
}
