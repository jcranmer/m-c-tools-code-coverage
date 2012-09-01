#!/usr/bin/python

import ftplib
import json
import os
import re
import select
import subprocess
import sys
import threading
import urllib2

from optparse import OptionParser

PlatformConfig = {}
PlatformConfig['linux64'] = {
  'package': '.tar.bz2',
  'tbpl': 'fedora64'
}

TestConfig = {}
TestConfig['.*-crashtest-.*\\.txt\\.gz'] = {
  'symbol': 'R-C',
  'tbpl': 'crashtest'
}
TestConfig['.*-jsreftest-.*\\.txt\\.gz'] = {
  'symbol': 'R-J',
  'tbpl': 'jsreftest'
}
TestConfig['.*-mochitests-1-.*\\.txt\\.gz'] = {
  'symbol': 'M-1',
  'tbpl': 'mochitests-1'
}
TestConfig['.*-mochitests-2-.*\\.txt\\.gz'] = {
  'symbol': 'M-2',
  'tbpl': 'mochitests-2'
}
TestConfig['.*-mochitests-3-.*\\.txt\\.gz'] = {
  'symbol': 'M-3',
  'tbpl': 'mochitests-3'
}
TestConfig['.*-mochitests-4-.*\\.txt\\.gz'] = {
  'symbol': 'M-4',
  'tbpl': 'mochitests-4'
}
TestConfig['.*-mochitests-5-.*\\.txt\\.gz'] = {
  'symbol': 'M-5',
  'tbpl': 'mochitests-5'
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
TestConfig['.*-jetpack-.*\\.txt\\.gz'] = {
  'symbol': 'JP',
  'tbpl': 'jetpack'
}
TestConfig['[^_]*\\.txt\\.gz'] = {
  'symbol': 'B',
  'tbpl': 'check'
}

def extractFromTarball(tarball, platformdir):
  # XXX: Use tarfile instead of subprocesses
  tarxj = subprocess.Popen(["tar", "-xj", "-C", platformdir, "firefox/gcno.tar.bz2", "-f", os.path.join(platformdir, tarball)])
  tarxj.wait()
  os.rename(os.path.join(platformdir, "firefox", "gcno.tar.bz2"),
      os.path.join(platformdir, "gcno.tar.bz2"))

class PrefixerThread(threading.Thread):
  # Create an object to poll on!
  _poller = select.poll()
  # fd -> (file object, prefix)
  _prefixes = {}
  # Lock for the map
  _guard = threading.Lock()
  # Stop
  _stop = False

  def addPipe(self, fdobj, prefix):
    fd = fdobj.fileno()
    with self._guard:
      self._poller.register(fd)
      self._prefixes[fd] = (fdobj, prefix)

  def shutdown(self):
    with self._guard:
      self._stop = True

  def run(self):
    while True:
      # Have we been told to stop?
      with self._guard:
        if self._stop:
          break
      for fd, event in self._poller.poll(1000):
        if event & select.POLLIN:
          with self._guard:
            fileobj, prefix = self._prefixes[fd]
          sys.stdout.write('%s%s' % (prefix, fileobj.readline()))
        elif event & select.POLLHUP:
          with self._guard:
            self._poller.unregister(fd)
            del self._prefixes[fd]

testre = re.compile('try_(.*)_test-(.*)')

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

  # Utilities for the download process
  savedir = ftp.pwd()
  procs = []
  prefixer = PrefixerThread()
  prefixer.start()
  try:
    for ftpplatformdir in ftp.nlst():
      # Erase any cd commands done over FTP
      ftp.cwd(savedir)

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
      ftp.cwd(ftpplatformdir)
      files = ftp.nlst()
      package = filter(lambda f: f[-len(config['package']):] == config['package'],
        files)
      if len(package) != 1:
        print "Couldn't find package: %s" % str(package)
        break
      package = package[0]
      gcnoloc = os.path.join(platformdir, "gcno.tar.bz2")
      if not os.path.exists(gcnoloc):
        print "Downloading package for %s" % prettyname

        # Ideally, we'd want to pipe this, but tar seems to be having some
        # problems with pipes, so we'll just save this to an actual file.
        tarloc = os.path.join(platformdir, package)
        write = open(tarloc, 'wb')
        ftp.retrbinary("RETR %s" % package, lambda block : write.write(block))
        write.close()

        # Extract gcno.tar.ba2 from the package
        extractFromTarball(package, platformdir)

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

        if not os.path.exists(os.path.join(platformdir, log)):
          locallog = open(os.path.join(platformdir, log), 'wb')
          print "Retrieving log %s for test %s" % (log, tconfig['symbol'])
          ftp.retrbinary("RETR %s" % log, lambda block : locallog.write(block))
          locallog.close()
        # XXX: Move unpack-gcda.sh scripts into python implementation?
        #unpacker = subprocess.Popen(["./unpack-gcda.sh",
        #  os.path.join(platformdir, log), "/tmp/firefox/gcno.tar.bz2"],
        #  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        #prefixer.addPipe(unpacker.stdout, "[" + tconfig['symbol'] + "] ")
        #prefixer.addPipe(unpacker.stderr, "[" + tconfig['symbol'] + ":err] ")
        #procs.append(unpacker)
    ftp.quit()
    for proc in procs:
      proc.wait()
  finally:
    prefixer.shutdown()

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
