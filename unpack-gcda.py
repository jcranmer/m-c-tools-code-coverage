#!/usr/bin/python

import os
import re
import sys
import shutil
import tempfile
import subprocess
from optparse import OptionParser

# Rules to translate log filenames to test suite names
testFileToName = {
  '.*-(crashtest|jsreftest|reftest|xpcshell)-.*\.txt\.gz$' : '$1',
  '.*-mochitests-(\d)-.*\.txt\.gz$' : 'mochitests-$1',
  '.*-mochitest-other-.*\.txt\.gz$' : [
                                        'mochitest-chrome', 
                                        'mochitest-browser-chrome', 
                                        'mochitest-a11y',
                                        'mochitest-ipcplugins'
                                      ],
  #'.*-(linux|macosx).*\.txt\.gz$' : 'check'    
  '.*-(linux|macosx).*-try\d+-build\d+\.txt\.gz$' : 'check'
}

def parseOpts():
  usage = 'Usage: %prog [options] <testLog> <path to gcno.tar.bz2>'
  parser = OptionParser(usage)
  # See http://docs.python.org/library/optparse.html#optparse.OptionParser.disable_interspersed_args
  parser.disable_interspersed_args()

  # Define the output base directory.
  parser.add_option('-o', '--output-dir',
                    dest='outputDir',
                    default="/tmp/gcda-unpacked",
                    help='Output directory for info and log files.')

  parser.add_option('-d', '--debug',
                    dest='debug',
                    action='store_true',
                    default=False,
                    help='Debugging mode, be more verbose and don\'t delete temporary files.')

  parser.add_option('-c', '--ccov',
                    dest='ccovPath',
                    default='/src/mozilla-tools/mozilla-coverage/ccov.py',
                    help='Path to CCOV script.')

  parser.add_option('-g', '--gcov-version',
                    dest='gcovVersion',
                    default='4.5',
                    help='GCOV version to specify when calling LCOV.')


  (options, args) = parser.parse_args()

  if len(args) < 2:
      parser.error('Not enough arguments')

  return (options, args)

def main():
  # Script options
  (options, args) = parseOpts()
  testLog = os.path.abspath(args[0])
  gcnoPath = os.path.abspath(args[1])

  if not os.path.exists(testLog):
    raise Exception("File not found: " + testLog)

  if not os.path.exists(gcnoPath):
    raise Exception("File not found: " + gcnoPath)

  processLog(testLog, gcnoPath, options)


def processLog(testLog, gcnoPath, options):
  if options.debug:
    print "Processing log file " + testLog

  if not testLog.endswith(".txt.gz"):
    raise Exception("Unknown test log format: " + testLog)

  testName = None
  
  # Find proper test name
  for (regex, name) in testFileToName.items():
    match = re.match(regex, testLog)
    if match != None:
      testName = name

      # Implicitely convert string to a one-element list
      if isinstance(testName, str):
        testName = [ testName ]

      # Manually substitute $1 by first group match
      groups = match.groups()
      if len(groups) > 0:
          testName = map(lambda w: w.replace('$1', groups[0]), testName)
      
      break

  if options.debug:
    print "Expected test(s): " + str(testName)

  if testName == None:
    raise Exception("Unknown test suite: " + testLog)

  outDir = options.outputDir
  unpackDir = tempfile.mkdtemp("unpack-gcda")

  if options.debug:
    print "Using temporary directory " + unpackDir
  
  gunzipProc = subprocess.Popen(['gunzip', '-c', testLog], stdout=subprocess.PIPE)
  delimiter = '~!@!~!@!~!@!~!@!~!@!~!@!~!@!~!@!~'
  inBase64 = False
  idxBase64 = 0
  blobsBase64 = []
  skipLines = 0

  for line in iter(gunzipProc.stdout.readline,''):
    if skipLines > 0:
      skipLines -= 1
      continue

    if inBase64:
      if delimiter in line:
        inBase64 = False
      else:
        
        # FIXME: Hack to remove stderr output from base64 blob
        matchWarn = re.match("^(.*?)WARNING:.*$", line)
        if matchWarn != None:
          line = matchWarn.group(1)
          skipLines = 7 # Skip next 7 lines

        blobsBase64[-1].append(line)
    elif delimiter in line:
        inBase64 = True
        blobsBase64.append([])

  # Mismatch in the number of expected vs. actual blobs?
  if len(blobsBase64) != len(testName):
    raise Exception("Expected " + str(len(testName)) + " blobs, but got " + str(len(blobsBase64)) + " while processing: " + testLog)

  # Process every base64 block we extracted
  for idx, blob in enumerate(blobsBase64):
    # Write base64 blob to file
    outFilename = os.path.join(unpackDir, "results-" + str(idx) + ".bin")
    outFile = open(outFilename, "w")
    outFile.writelines(blob)
    outFile.close()
    
    # Create test directory and logfile
    testDir = os.path.join(unpackDir, "unpacked-" + str(idx))
    os.mkdir(testDir)
    logFilename = os.path.join(outDir, testName[idx] + ".log")
    logFile = open(logFilename, "w")

    # Unpack extracted archives
    subprocess.check_call(['base64 -d ' + outFilename + ' | tar -xj -C ' + testDir], shell=True, stdout=logFile, stderr=logFile)

    # FIXME: This is necessary for lcov/gcov to work properly
    subprocess.check_call(['find', testDir, '-name', 'jchuff.gcda', '-delete'], stdout=logFile, stderr=logFile)
    
    # Traverse directory structure down to the actual build directory
    oldcwd = os.getcwd()
    dirList = [ testDir ]
    while (len(dirList) == 1 and os.path.isdir(dirList[0])):
      os.chdir(dirList[0])
      dirList = os.listdir(".")

    # Unpack gcno tarball
    subprocess.check_call(['tar', '-xjf', gcnoPath])

    preFile = os.path.join(unpackDir, "test-" + str(idx) + "-pre.info")

    # Run lcov and ccov
    subprocess.check_call([ 'lcov', '-c', '-d', '.', '-o', preFile, '--gcov-tool=gcov-' + options.gcovVersion, '-t', testName[idx] ], stdout=logFile, stderr=logFile)
    subprocess.check_call([ options.ccovPath, '-a', preFile, '-e', '/builds/*', '-o', os.path.join(outDir, testName[idx] + '.info') ], stdout=logFile, stderr=logFile)

    os.chdir(oldcwd)

    logFile.close()
  
  if not options.debug:
    shutil.rmtree(unpackDir)

if __name__ == '__main__':
  main()

