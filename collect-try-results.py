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

GCNO_EXTS = ['code-coverage-gcno.zip', 'code-coverage-gcno.nzip']

# This is a map of builder filenames to (display name, hidden) tuples.
builder_data = dict()

ccov = None
def loadConfig(job):
    name = job['ref_data_name'].split(' ')[-1]
    result = re.match('^(.*?)-?([0-9+])$', name)
    if result is not None:
        test = result.group(1)
    else:
        test = name
    return {
        'test': [test],
        'name': [name],
        'shortname': shortName(job)
    }

def loadJSON(uri):
    return json.load(urllib2.urlopen("http://treeherder.mozilla.org" + uri))

def shortName(job):
    return "%(platform)s %(job_group_symbol)s-%(job_type_symbol)s" % job

def downloadTreeherder(revision, outdir):
    print("Loading data from treeherder")
    # Grab the result_set_id for the jobs query
    resultid = loadJSON("/api/project/try/resultset/" +
        "?revision=" + revision)['results'][0]['id']
    # Load the list of jobs from treeherder
    data = loadJSON(
        "/api/project/try/jobs/?count=2000&return_type=list&result_set_id=%d"
        % resultid)
    remap = data['job_property_names']
    platforms = dict()
    for job in data['results']:
        job = dict(zip(remap, job))
        # Ignore unfinished jobs
        if job['state'] != 'completed':
            print '%s has not completed, ignoring' % shortName(job)
            continue
        # Grab some interesting job info
        job['info'] = loadJSON(
            "/api/project/try/artifact/?job_id=%d&name=Job+Info" % job['id']
            )[0]['blob']
        platform = "%(platform)s-%(platform_option)s" % job
        platforms.setdefault(platform, []).append(job)

    info_files = []
    # For each platform, work out the corresponding FTP dir
    for pname in platforms:
        print('Processing platform %s' % pname)
        if pname.startswith('android'): continue # XXX
        elif pname.startswith('osx'): continue # XXX
        elif 'b2g' in pname: continue # XXX (need taskcluster goodness)
        elif 'mulet' in pname: continue # XXX (need taskcluster goodness)
        jobs = platforms[pname]

        # Grab the directory of a log and spit out the FTP dir.
        logfile = jobs[0]['info']['logurl']
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
        collector = CoverageCollector(platformdir, pname, ftp)
        collector.downloadNotes()

        for job in jobs:
            # Ignore builder jobs here.
            if job['job_type_symbol'] == 'B':
                continue
            info_files += collector.processJob(job)

    # Now that we have all of the info files, combine these into a master file.
    total = os.path.join(outdir, 'all.info')
    # Dummy to touch the file
    with open(total, 'w') as tmp:
        pass
    # Do this slowly and one at a time, since the master file is going to grow
    # very large.
    for x in info_files:
        subprocess.check_call([ccov, '-a', total, '-a', x, '-o', total])

class CoverageCollector(object):
    def __init__(self, localdir, platform, ftp):
        self.localdir = localdir
        self.ftp = ftp
        self.platformdir = ftp.pwd().split('/')[-1]
        self.platform = platform

    def downloadNotes(self):
        files = self.ftp.nlst()

        # First, find the gcno data.
        for ext in GCNO_EXTS:
            package = filter(lambda f: f.endswith(ext), files)
            if len(package) == 1:
                break
        else:
            print 'Did not find files in FTP directory for %s' % self.platform
            return
        self.gcnotar = os.path.join(self.localdir, 'gcno.zip')
        if not os.path.exists(self.gcnotar):
            print "Downloading package for %s" % self.platform
            with open(self.gcnotar, 'wb') as write:
                self.ftp.retrbinary("RETR %s" % package[0],
                    lambda block : write.write(block))

        self.ftp.quit()

    def processJob(self, job):
        tconfig = loadConfig(job)

        # Find all of the gcda artifacts.
        artifacts = dict()
        for a in job['info']['job_details']:
            if 'title' not in a:
                continue
            if a['title'] != 'artifact uploaded': continue
            artifacts[a['value']] = a['url']
        files = filter(lambda x: re.match('.*gcda.*\.zip', x), artifacts)
        files.sort()
        if len(files) == 0:
            print("No coverage data for %s" % tconfig['shortname'])
            return []

        # Map test names to artifact URLs
        if len(files) != len(tconfig['name']):
            print("Mismatch for test %s" % tconfig['shortname'])
            return []
        files = ((f, artifacts[f]) for f in files)

        # Download those gcda files as appropriate
        written = []
        for data, tname, cname in zip(files, tconfig['test'], tconfig['name']):
            name, url = data
            localname = os.path.join(self.localdir, cname + '-' + name)
            written.append(os.path.join(self.localdir, cname + '.info'))
            if not os.path.exists(localname):
                print "Retrieving %s for test %s" % (name, cname)
                urllib.urlretrieve(url, localname)
            with zipfile.ZipFile(localname) as fd:
                self.computeCoverage(fd, cname, tname)
        return written

    def computeCoverage(self, tchunknamezip, tchunkname, test):
        lcovpre = os.path.join(self.localdir, tchunkname + '-pre.info')
        lcovname = os.path.join(self.localdir, tchunkname + '.info')
        lcovlog = os.path.join(self.localdir, tchunkname + '.log')
        # Don't recompute it if we already did it.
        if os.path.exists(lcovname):
            return

        # Unpack the gcda directory
        unpackDir = tempfile.mkdtemp("unpack-gcda")
        tchunknamezip.extractall(unpackDir)

        # Find the corresponding gcda basedir
        basedir = unpackDir
        while True:
            files = os.listdir(basedir)
            if len(files) > 1: break
            if len(files) == 0:
                print "Empty gcda directory!?"
                return
            sub = os.path.join(basedir, files[0])
            if not os.path.isdir(sub): break
            basedir = sub

        # Copy the gcno files over
        with zipfile.ZipFile(self.gcnotar) as gcnofd:
            gcnofd.extractall(basedir)

        # Delete jchuff.gcda. This causes an infinite loop in gcov for some
        # as-yet unknown reason.
        for dirpath, dirnames, fnames in os.walk(basedir):
            if 'jchuff.gcda' in fnames:
                os.remove(os.path.join(dirpath, 'jchuff.gcda'))

        # Run lcov to compute the output lcov file
        with open(lcovlog, 'w') as logfile:
            subprocess.check_call([ccov, '-c', basedir, '-o', lcovpre,
                '-t', test, '--gcov-tool', 'gcov-4.7'],
                stdout=logfile, stderr=subprocess.STDOUT)

            # Reprocess the file to only include m-c source code and exclude
            # things like /usr/include/c++/blah
            subprocess.check_call([ccov, '-a', lcovpre, '-e', '/builds/*',
                '-o', lcovname], stdout=logfile, stderr=subprocess.STDOUT)

            # Normalize the prefix names to the mozilla-central directory
            subprocess.check_call(['sed', '-e',
                's+^SF:/builds/slave/[^/]*/build/src/+SF:+', '-i', lcovname])

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
