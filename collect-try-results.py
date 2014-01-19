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

TestConfig = {}
TestConfig['.*-crashtest-.*\\.txt\\.gz'] = {
    'symbol': 'R-C',
    'tbpl': 'crashtest'
}
TestConfig['.*-jetpack-.*\\.txt\\.gz'] = {
    'symbol': 'JP',
    'tbpl': 'jetpack'
}
TestConfig['.*-jsreftest-.*\\.txt\\.gz'] = {
    'symbol': 'R-J',
    'tbpl': 'jsreftest'
}
TestConfig['.*-mochitest-1-.*\\.txt\\.gz'] = {
    'symbol': 'M-1',
    'tbpl': 'mochitest-1'
}
TestConfig['.*-mochitest-2-.*\\.txt\\.gz'] = {
    'symbol': 'M-2',
    'tbpl': 'mochitest-2'
}
TestConfig['.*-mochitest-3-.*\\.txt\\.gz'] = {
    'symbol': 'M-3',
    'tbpl': 'mochitest-3'
}
TestConfig['.*-mochitest-4-.*\\.txt\\.gz'] = {
    'symbol': 'M-4',
    'tbpl': 'mochitest-4'
}
TestConfig['.*-mochitest-5-.*\\.txt\\.gz'] = {
    'symbol': 'M-5',
    'tbpl': 'mochitest-5'
}
TestConfig['.*-mochitest-other-.*\\.txt\\.gz'] = {
    'symbol': 'M-oth',
    'tbpl': 'mochitest-other'
}
TestConfig['.*-reftest-.*\\.txt\\.gz'] = {
    'symbol': 'R-R',
    'tbpl': 'reftest'
}
TestConfig['.*-xpcshell-.*\\.txt\\.gz'] = {
    'symbol': 'X',
    'tbpl': 'xpcshell'
}
TestConfig['[^_]*\\.txt\\.gz'] = {
    'symbol': 'B',
    'tbpl': 'check'
}

testre = re.compile('try_(.*)_test-(.*)')
uploadre = re.compile('Uploaded (gcda.*\.zip) to (https?://\S*)')

def downloadTestResults(ftpName, outdir):
    # What builds are hidden?
    builders = json.load(urllib2.urlopen(
      "https://tbpl.mozilla.org/php/getBuilders.php?branch=Try"))
    tbplData = {}
    for build in builders:
      match = testre.match(build['name'])
      if match is not None:
        tbplData.setdefault(match.group(1), {})[match.group(2)] = build['hidden']
      else:
        names = build['name'].split('-')
        isdebug = names[-1] == 'debug'
        if isdebug:
          platform = '-'.join(names[1:-1])
        else:
          platform = '-'.join(names[1:])
        if platform in PlatformConfig:
          pseudo = PlatformConfig[platform]['tbpl'] + ['', '-debug'][isdebug]
          tbplData.setdefault(pseudo, {})['check'] = build['hidden']

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
      config = PlatformConfig[platform]
      hiddenlog = tbplData[config['tbpl'] + ["", "-debug"][isdebug]]

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
      break

      # Now that we have the package and the gcno data extracted, let's extract
      # data from all of the test logs
      logs = (f for f in files if f[-7:] == ".txt.gz")
      for log in logs:
        # Which test config should we use?
        tconfig = None
        for pattern in TestConfig:
          if re.match(pattern, log):
            tconfig = TestConfig[pattern]
            break
        if tconfig is None:
          print "Unknown config for log file %s" % log
          break

        # Is this log hidden?
        if hiddenlog[tconfig['tbpl']]:
          print "Ignoring hidden build %s-%s" % (prettyname, tconfig['tbpl'])
          continue

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

        # First, find the package
        config = PlatformConfig[self.platform]
        package = filter(lambda f: f.endswith(config['package']), files)[0]
        localpkg = os.path.join(self.localdir, package)
        if not os.path.exists(localpkg):
            with open(localpkg, 'wb') as write:
                self.ftp.retrbinary("RETR %s" % package,
                    lambda block : write.write(block))

        # Now process all of the log files
        # First, download the log files (the FTP connection may timeout if we
        # don't do this first)
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
        self.unpackPackage(localpkg)
        for log in logs:
            self.downloadLog(log)

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
        tconfig = None
        for pattern in TestConfig:
            if re.match(pattern, log):
                tconfig = TestConfig[pattern]
                break
        if tconfig is None:
            print "Unknown config for log file %s" % log
            return

        # XXX: Figure out if the test is hidden

        # Retrieve the log of interest
        logfile = os.path.join(self.localdir, log)
        if not os.path.exists(logfile):
            print "Why haven't we downloaded a log yet?"
            return

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

        # Download those gcda files as appropriate
        for name, url in files:
            localname = os.path.join(self.localdir,
                tconfig['tbpl'] + '-' + name)
            if not os.path.exists(localname):
                print "Retrieving %s for test %s" % (name, tconfig['symbol'])
                urllib.urlretrieve(url, localname)
            with zipfile.ZipFile(localname) as fd:
                self.computeCoverage(fd, tconfig['tbpl'])

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
        lcovname = os.path.join(self.localdir, test + '.info')
        subprocess.check_call(['lcov', '-c', '-d', basedir, '-o', lcovname,
            '-t', test])

        shutil.rmtree(unpackDir)


def main(argv):
    parser = OptionParser('Usage: %prog [options] username revision')
    parser.disable_interspersed_args()
    parser.add_option('-o', '--output-dir', dest='outputDir',
                      default="/tmp/output",
                      help="Output directory for .info files")
    (options, args) = parser.parse_args(argv)
    if len(args) < 3:
        parser.error('Not enough arguments')

    downloadTestResults(args[1] + '-' + args[2], options.outputDir)

if __name__ == '__main__':
    main(sys.argv)
