#!/usr/bin/python

import ftplib
import gzip
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib, urllib2
import zipfile

from optparse import OptionParser

PlatformConfig = {}
PlatformConfig['linux64'] = {
    'package': '.tar.bz2',
    'tbpl': 'ubuntu64_vm'
}
PlatformConfig['linux'] = {
    'package': '.tar.bz2',
    'tbpl': 'ubuntu32_vm'
}

TestConfig = {}
TestConfig['cppunit'] = {'symbol': 'Cpp'}
TestConfig['crashtest'] = {'symbol': 'R-C'}
TestConfig['crashtest-ipc'] = {'symbol': 'R-Cipc'}
TestConfig['jetpack'] = {'symbol': 'JP'}
TestConfig['jsreftest'] = {'symbol': 'R-J'}
TestConfig['mochitest-1'] = {'symbol': 'M-1'}
TestConfig['mochitest-2'] = {'symbol': 'M-2'}
TestConfig['mochitest-3'] = {'symbol': 'M-3'}
TestConfig['mochitest-4'] = {'symbol': 'M-4'}
TestConfig['mochitest-5'] = {'symbol': 'M-5'}
TestConfig['mochitest-browser-chrome'] = {'symbol': 'M-bc'}
TestConfig['mochitest-other'] = {
    'symbol': 'M-oth',
    'tbpl': ['mochitest-chrome', 'mochitest-a11y', 'mochitest-plugins']
}
TestConfig['reftest'] = {'symbol': 'R-R'}
TestConfig['reftest-ipc'] = {'symbol': 'R-Ripc'}
TestConfig['reftest-no-accel'] = {'symbol': 'R-Ru'}
TestConfig['xpcshell'] = {'symbol': 'X'}
TestConfig['build'] = {
    'symbol': 'B',
    'tbpl': ['check']
}

uploadre = re.compile('Uploaded (gcda.*\.zip) to (https?://\S*)')

# This is a map of builder filenames to (display name, hidden) tuples.
builder_data = dict()

ccov = None
def downloadTestResults(ftpName, outdir):
    # Load builder data from tbpl.
    builders = json.load(urllib2.urlopen(
        "https://tbpl.mozilla.org/php/getBuilders.php?branch=Try"))
    for build in builders:
        builder_data[build['name']] = (build['buildername'], build['hidden'])

    # Find builds on the try server
    ftp = ftplib.FTP("ftp.mozilla.org")
    ftp.login()
    ftp.cwd("pub/mozilla.org/firefox/try-builds/" + ftpName)
    platforms = ftp.nlst()
    ftp.quit()

    # Utilities for the download process
    for ftpplatformdir in platforms:
      # Extract the platform from the directory name.
      # For try, this is try-<platform>[-debug]
      # try-c-c has try-comm-central-<platform>[-debug]
      dircomps = ftpplatformdir.split('-')
      isdebug = dircomps[-1] == "debug"
      platform = isdebug and dircomps[-2] or dircomps[-1]
      prettyname = platform + ["", "-debug"][isdebug]
      if platform not in PlatformConfig:
        print "Unknown platform: %s" % (platform)
        print "Ignoring..."
        break

      # Make a local directory to download all of the files to
      platformdir = os.path.join(outdir, ftpplatformdir)
      if not os.path.exists(platformdir):
        os.makedirs(platformdir)

      # Extract files from the FTP server.
      ftp = ftplib.FTP("ftp.mozilla.org")
      ftp.login()
      ftp.cwd("pub/mozilla.org/firefox/try-builds/" + ftpName)
      ftp.cwd(ftpplatformdir)
      collector = CoverageCollector(platformdir, ftp)
      collector.findFiles()

class CoverageCollector(object):
    def __init__(self, localdir, ftp):
        self.localdir = localdir
        self.ftp = ftp
        self.platformdir = ftp.pwd().split('/')[-1]
        self.platform = self.platformdir.split('-')[-1]
        if self.platform == 'debug':
            self.isDebug = True
            self.platform = self.platformdir.split('-')[-2]
        else:
            self.isDebug = False

    def findFiles(self):
        files = self.ftp.nlst()

        # First, find the gcno data.
        config = PlatformConfig[self.platform]
        package = filter(lambda f: f == 'all-gcno.tbz2', files)[0]
        self.gcnotar = os.path.join(self.localdir, 'gcno.tar.bz2')
        if not os.path.exists(self.gcnotar):
            print "Downloading package for %s" % self.platform
            with open(self.gcnotar, 'wb') as write:
                self.ftp.retrbinary("RETR %s" % package,
                    lambda block : write.write(block))

        # Download the log files before processing (the FTP connection may
        # timeout if we don't do this first)
        logs = [f for f in files if f.endswith('.txt.gz')]
        for log in logs:
            logfile = os.path.join(self.localdir, log)
            if os.path.exists(logfile):
                continue
            with open(logfile, 'wb') as locallog:
                print "Retrieving log %s" % log
                self.ftp.retrbinary("RETR %s" % log,
                    lambda block : locallog.write(block))
        self.ftp.quit()

        # Now actually process the logs
        combined = []
        for log in logs:
            combined += self.downloadLog(log)

        # Make the final platform coverage file
        args = [ccov]
        for sub in combined:
            args.append('-a')
            args.append(sub)
        args += ['-o', os.path.join(self.localdir, 'all.info')]
        print args
        subprocess.check_call(args)

    def unpackPackage(self, package):
        print "Unpacking package for %s" % self.platform
        # Output the package into a seperate gcno directory
        self.gcnotar = os.path.join(self.localdir, 'gcno.tar.bz2')
        with tarfile.open(package) as tarball:
            gcnoentry = filter(lambda f: f.name.endswith('gcno.tar.bz2'),
                tarball.getmembers())[0]
            innerfd = tarball.extractfile(gcnoentry)
            with open(self.gcnotar, 'wb') as gcnofd:
                gcnofd.write(innerfd.read())

    def downloadLog(self, log):
        # Which test config should we use?

        # The log file name is going to be
        # <builder>-bm##-<platform>-build#.txt.gz. We only care about the first
        # portion of this string.
        builder = log[:log.find("-bm")]
        prettyname = builder_data[builder][0].split(' ')
        # prettyname is something like Ubuntu VM 12.04 try opt test mochitest-1
        testname = prettyname[-1]
        try:
            tconfig = TestConfig[testname]
        except KeyError:
            print "Unknown config for test %s" % testname
            return []

        # XXX: Figure out if the test is hidden

        # Retrieve the log of interest
        logfile = os.path.join(self.localdir, log)
        if not os.path.exists(logfile):
            print "Why haven't we downloaded a log yet?"
            return []

        # Find the location of the gcda blobs
        files = []
        with gzip.open(logfile, 'rb') as gziplog:
            for line in gziplog:
                if 'TinderboxPrint' not in line: continue
                match = uploadre.search(line)
                if match is None: continue
                files.append((match.group(1), match.group(2)))

        if len(files) == 0:
            print "Could not find any data for test %s" % tconfig['symbol']
            return []

        # Build specific names for each file
        testnames = tconfig.get('tbpl', [testname])
        if len(files) != len(testnames):
            print "Found %d tests, expected %d for test %s" % (
                len(files), len(testnames), tconfig['symbol'])
            return []

        # Download those gcda files as appropriate
        written = []
        for data, tname in zip(files, testnames):
            name, url = data
            localname = os.path.join(self.localdir, tname + '-' + name)
            written.append(os.path.join(self.localdir, tname + '.info'))
            if not os.path.exists(localname):
                print "Retrieving %s for test %s" % (name, tname)
                urllib.urlretrieve(url, localname)
            with zipfile.ZipFile(localname) as fd:
                self.computeCoverage(fd, tname)

        return written

    def computeCoverage(self, testzip, test):
        # Unpack the gcda directory
        unpackDir = tempfile.mkdtemp("unpack-gcda")
        testzip.extractall(unpackDir)

        # Find the corresponding gcda basedir
        basedir = unpackDir
        while True:
            files = os.listdir(basedir)
            if len(files) > 1: break
            sub = os.path.join(basedir, files[0])
            if not os.path.isdir(sub): break
            basedir = sub

        # Copy the gcno files over
        with tarfile.open(self.gcnotar) as gcnofd:
            gcnofd.extractall(basedir)

        # Run lcov to compute the output lcov file
        lcovpre = os.path.join(self.localdir, test + '-pre.info')
        lcovname = os.path.join(self.localdir, test + '.info')
        lcovlog = os.path.join(self.localdir, test + '.log')
        with open(lcovlog, 'w') as logfile:
            subprocess.check_call(['lcov', '-c', '-d', basedir, '-o', lcovpre,
                '-t', test + '-' + self.platformdir, '--gcov-tool', 'gcov-4.7'],
                stdout=logfile, stderr=subprocess.STDOUT)

            # Reprocess the file to only include m-c source code and exclude
            # things like /usr/include/c++/blah
            subprocess.check_call([ccov, '-a', lcovpre, '-e', '/builds/*',
                '-o', lcovname], stdout=logfile, stderr=subprocess.STDOUT)

            # Remove the original lcov file.
            os.remove(lcovpre)

        shutil.rmtree(unpackDir)


def main(argv):
    directory = os.path.dirname(os.path.realpath(__file__))
    parser = OptionParser('Usage: %prog [options] username revision')
    parser.disable_interspersed_args()
    parser.add_option('-o', '--output-dir', dest='outputDir',
                      default="/tmp/output",
                      help="Output directory for .info files")
    parser.add_option('-c', '--ccov-path', dest='ccovExe',
                      default=os.path.join(directory, '..',
                          "mozilla-coverage", "ccov.py"),
                      help="Output directory for .info files")
    (options, args) = parser.parse_args(argv)
    if len(args) < 3:
        parser.error('Not enough arguments')

    global ccov
    ccov = options.ccovExe
    downloadTestResults(args[1] + '-' + args[2], options.outputDir)

if __name__ == '__main__':
    main(sys.argv)
