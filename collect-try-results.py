#!/usr/bin/python2

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib, urllib2
import zipfile

from optparse import OptionParser

GCNO_EXTS = ['code-coverage-gcno.zip']

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

def find_data_sources(revision):
    ''' Return a dictionary of platform names and relevant file information for
    the given revision that was pushed to try. '''
    print("Loading data from treeherder")
    # Grab the result_set_id for the jobs query
    resultid = loadJSON("/api/project/try/resultset/" +
        "?revision=" + revision)['results'][0]['id']

    # Load the list of jobs from treeherder
    data = loadJSON(
        "/api/project/try/jobs/?count=2000&return_type=list&result_set_id=%d"
        % resultid)
    remap = data['job_property_names']
    platform_jobs = dict()
    for job in data['results']:
        job = dict(zip(remap, job))

        # Ignore unfinished jobs
        if job['state'] != 'completed':
            print '%s has not completed, ignoring' % shortName(job)
            continue

        platform = "%(platform)s-%(platform_option)s" % job
        platform_jobs.setdefault(platform, []).append(job)

    platforms = dict()
    for platform in platform_jobs:
        jobs = platform_jobs[platform]
        builders = filter(lambda j: j['job_type_symbol'] == 'B', jobs)

        # Find the last builder
        buildbot_builder = None
        taskcluster_builder = None
        for j in builders:
            # Only consider builds that succeeded
            if j['result'] not in ('testfailed', 'success'):
                continue
            if j['build_system_type'] == 'buildbot':
                buildbot_builder = j
            elif j['build_system_type'] == 'taskcluster':
                taskcluser_builder = j

        if buildbot_builder is not None:
            test_jobs = filter(
                lambda j: j['build_system_type'] == 'buildbot' and
                j['job_type_symbol'] != 'B', jobs)
            platforms[platform] = BuildbotFilesFinder(platform,
                buildbot_builder, test_jobs)

        # XXX: Handle taskcluster-based builds

    return platforms

class BuildbotFilesFinder(object):
    def __init__(self, platform, builderjob, testjobs):
        self.platform = platform
        self.builder = builderjob
        self.jobs = testjobs

    def _load_treeherder_info(self, job):
        return loadJSON(
            "/api/project/try/artifact/?job_id=%d&name=Job+Info" % job['id']
        )[0]['blob']

    def get_build_artifacts(self):
        # Sigh, so the simplest way to do this (since the archives are no longer
        # hosted on an actual FTP server) is to scrape the URL. Thrilling.
        import html5lib
        logurl = self._load_treeherder_info(self.builder)['logurl']
        # Capturing the / at the end is critical if we want to not get an error
        # page.
        fd = urllib2.urlopen(logurl[:logurl.rfind('/') + 1])
        try:
            doc = html5lib.parse(fd, namespaceHTMLElements=False)
        finally:
            fd.close()

        # At this point, the page is a simple table and all the links are in
        # that table; the only extraneous link is the .. parent directory.
        files = []
        for link in doc.findall(".//a[@href]"):
            if link.text == '..':
                continue
            files.append(urllib.basejoin(logurl, link.get("href")))
        return files

    def get_test_artifacts(self, job):
        details = self._load_treeherder_info(job)['job_details']
        results = []
        for note in details:
            if 'title' in note and note['title'] == 'artifact uploaded':
                results.append((note['value'], note['url']))
        return results

def collect_all_coverage(platforms, outdir):
    '''Download all the coverage data for all platforms and store the results
    in outdir/all.info.'''
    info_files = []
    # For each platform, work out the corresponding FTP dir
    for pname in platforms:
        print('Processing platform %s' % pname)

        # Several platforms we ignore for the moment.
        if pname.startswith('android'): continue # XXX
        elif pname.startswith('osx'): continue # XXX
        elif 'b2g' in pname: continue # XXX (need taskcluster goodness)
        elif 'mulet' in pname: continue # XXX (need taskcluster goodness)

        data_source = platforms[pname]

        # Make a local directory to download all of the files to.
        platformdir = os.path.join(outdir, pname)
        if not os.path.exists(platformdir):
            os.makedirs(platformdir)

        # Extract files from the FTP server.
        collector = CoverageCollector(platformdir, pname, data_source)
        collector.downloadNotes()

        for job in data_source.jobs:
            info_files += collector.processJob(job)

    # Now that we have all of the info files, combine these into a master file.
    total = os.path.join(outdir, 'all.info')
    ccov_args = [ccov]
    for x in info_files:
        ccov_args += ['-a', x]
    ccov_args += ['-o', total]
    subprocess.check_call(ccov_args)


class CoverageCollector(object):
    def __init__(self, localdir, platform, data_source):
        self.localdir = localdir
        self.data_source = data_source
        self.platform = platform

    def downloadNotes(self):
        # First, find the gcno source data file in the artifacts list.
        files = self.data_source.get_build_artifacts()
        for ext in GCNO_EXTS:
            package = filter(lambda f: f.endswith(ext), files)
            if len(package) == 1:
                break
        else:
            print 'Did not find files in FTP directory for %s' % self.platform
            return

        # Download the tarball if necessary.
        self.gcnotar = os.path.join(self.localdir, 'gcno.zip')
        if not os.path.exists(self.gcnotar):
            print "Downloading package for %s" % self.platform
            urllib.urlretrieve(package[0], self.gcnotar)

    def processJob(self, job):
        tconfig = loadConfig(job)

        # Find all of the gcda artifacts.
        files = self.data_source.get_test_artifacts(job)
        files = filter(lambda x: re.match('.*gcda.*\.zip', x[0]), files)
        if len(files) == 0:
            print("No coverage data for %s" % tconfig['shortname'])
            return []

        # Map test names to artifact URLs
        if len(files) != len(tconfig['name']):
            print("Mismatch for test %s" % tconfig['shortname'])
            return []

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

        print("Processing test %s" % tchunkname)

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
                '-t', test, '--gcov-tool', 'gcov-4.8'],
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

        # Clean up afterwards.
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
    data_sources = find_data_sources(args[1])
    collect_all_coverage(data_sources, options.outputDir)

if __name__ == '__main__':
    main(sys.argv)
