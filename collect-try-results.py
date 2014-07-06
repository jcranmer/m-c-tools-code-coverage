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

TestConfig = {}
TestConfig['Cpp'] = {'name': 'cppunit'}
TestConfig['Jit'] = {'name': 'jittest'}
TestConfig['JP'] = {'name': 'jetpack'}
TestConfig['M-1'] = {'name': 'mochitest-1'}
TestConfig['M-2'] = {'name': 'mochitest-2'}
TestConfig['M-3'] = {'name': 'mochitest-3'}
TestConfig['M-4'] = {'name': 'mochitest-4'}
TestConfig['M-5'] = {'name': 'mochitest-5'}
TestConfig['M-bc1'] = {'name': 'mochitest-browser-chrome-1'}
TestConfig['M-bc2'] = {'name': 'mochitest-browser-chrome-2'}
TestConfig['M-bc3'] = {'name': 'mochitest-browser-chrome-3'}
TestConfig['M-dt'] = {'name': 'mochitest-devtools'}
TestConfig['M-oth'] = {
    'name': ['mochitest-chrome', 'mochitest-a11y', 'mochitest-plugins']
}
TestConfig['M-e10s-M-e10s'] = {'name': 'mochitest-e10s'}
TestConfig['Mn'] = {'name': 'marionette'}
TestConfig['R-C'] = {'name': 'crashtest'}
TestConfig['R-Cipc'] = {'name': 'crashtest-ipc'}
TestConfig['R-J'] = {'name': 'jsreftest'}
TestConfig['R-R'] = {'name': 'reftest'}
TestConfig['R-Ripc'] = {'name': 'reftest-ipc'}
TestConfig['R-Ru'] = {'name': 'reftest-no-accel'}
TestConfig['X'] = {'name': 'xpcshell'}

# This is a map of builder filenames to (display name, hidden) tuples.
builder_data = dict()

ccov = None
def loadConfig(job):
    shortname = job['job_group_symbol'] + '-' + job['job_type_symbol']
    if shortname.startswith('?-'):
        shortname = shortname[2:]
    result = {'shortname': shortname}
    if shortname not in TestConfig:
        raise Exception("Unknown test %s" % shortname)
    result.update(TestConfig[shortname])
    if not isinstance(result['name'], list):
        result['name'] = [result['name']]
    return result

def loadJSON(uri):
    return json.load(urllib2.urlopen("http://treeherder-dev.allizom.org" + uri))

def downloadTreeherder(revision, outdir):
    # Load the list of jobs from treeherder
    data = json.load(urllib2.urlopen(
        "http://treeherder-dev.allizom.org/api/project/try/resultset/" +
        "?format=json&with_jobs=true&revision=" + revision))['results'][0]
    platforms = dict()
    for p in data['platforms']:
        jobs = []
        for g in p['groups']:
            jobs += g['jobs']
        platforms[p['name'] + '-' + p['option']] = jobs

    # For each platform, work out the corresponding FTP dir
    for pname in platforms:
        print('Processing platform %s' % pname)
        jobs = platforms[pname]
        if jobs[0]['result'] == 'busted':
            print "Job %s did not build correctly, skipping" % pname
            continue
        logfile = loadJSON(jobs[0]['resource_uri'])['logs'][0]['url']
        ftpdir = logfile[logfile.find('.org/') + 5:logfile.rfind('/')]
        ftpplatformdir = ftpdir[ftpdir.rfind('/') + 1:]

        # Make a local directory to download all of the files to
        platformdir = os.path.join(outdir, ftpplatformdir)
        if not os.path.exists(platformdir):
            os.makedirs(platformdir)

        # Extract files from the FTP server.
        ftp = ftplib.FTP("ftp.mozilla.org")
        ftp.login()
        ftp.cwd(ftpdir)
        collector = CoverageCollector(platformdir, ftp)
        collector.downloadNotes()

        for job in jobs[1:]:
            collector.processJob(job)

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

    def downloadNotes(self):
        files = self.ftp.nlst()

        # First, find the gcno data.
        package = filter(lambda f: f == 'all-gcno.tbz2', files)[0]
        self.gcnotar = os.path.join(self.localdir, 'gcno.tar.bz2')
        if not os.path.exists(self.gcnotar):
            print "Downloading package for %s" % self.platform
            with open(self.gcnotar, 'wb') as write:
                self.ftp.retrbinary("RETR %s" % package,
                    lambda block : write.write(block))

        self.ftp.quit()

    def processJob(self, job):
        tconfig = loadConfig(job)

        # Find all of the gcda artifacts.
        data = loadJSON(job['resource_uri'])
        artifact = filter(lambda x: x['name'] == 'Job Info',
            data['artifacts'])
        if len(artifact) == 0:
            print("Can't find results for %s, try again later?" %
                tconfig['shortname'])
            return
        ajson = loadJSON(artifact[0]['resource_uri'])
        artifacts = dict()
        for a in ajson['blob']['job_details']:
            if a['title'] != 'artifact uploaded': continue
            artifacts[a['value']] = a['url']
        files = filter(lambda x: re.match('gcda.*?.zip', x), artifacts)
        files.sort()
        if len(files) == 0:
            print("No coverage data for %s" % tconfig['shortname'])
            return

        # Map test names to artifact URLs
        if len(files) != len(tconfig['name']):
            print("Mismatch for test %s" % tconfig['shortname'])
            return
        files = ((f, artifacts[f]) for f in files)

        # Download those gcda files as appropriate
        written = []
        for data, tname in zip(files, tconfig['name']):
            name, url = data
            localname = os.path.join(self.localdir, tname + '-' + name)
            written.append(os.path.join(self.localdir, tname + '.info'))
            if not os.path.exists(localname):
                print "Retrieving %s for test %s" % (name, tname)
                urllib.urlretrieve(url, localname)
            with zipfile.ZipFile(localname) as fd:
                self.computeCoverage(fd, tname)

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
            #subprocess.check_call(['lcov', '-c', '-d', basedir, '-o', lcovpre,
            #    '-t', test + '-' + self.platformdir, '--gcov-tool', 'gcov-4.7'],
            #    stdout=logfile, stderr=subprocess.STDOUT)
            subprocess.check_call([ccov, '-c', basedir, '-o', lcovpre,
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
    parser = OptionParser('Usage: %prog [options] revision')
    parser.disable_interspersed_args()
    parser.add_option('-o', '--output-dir', dest='outputDir',
                      default="/tmp/output",
                      help="Output directory for .info files")
    parser.add_option('-c', '--ccov-path', dest='ccovExe',
                      default=os.path.join(directory, '..',
                          "mozilla-coverage", "ccov.py"),
                      help="Output directory for .info files")
    (options, args) = parser.parse_args(argv)
    if len(args) != 2:
        parser.error('Not enough arguments')

    global ccov
    ccov = options.ccovExe
    downloadTreeherder(args[1], options.outputDir)

if __name__ == '__main__':
    main(sys.argv)
