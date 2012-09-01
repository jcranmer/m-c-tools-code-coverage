#!/bin/bash

if [ -z "$1" -o -z "$2" ]; then
  echo "Usage: `basename $0` <test file> <path to gcno.tar.bz2>" 1>&2
  exit $E_NOARGS
fi

# TODO:
# 1. Find out hidden data on m-c/try (tbpl.mozilla.org/php/getBuilders.php)
case $1 in
  *-crashtest-*.txt.gz) TESTS[1]=crashtest;;
  *-jsreftest-*.txt.gz) TESTS[1]=jsreftest;;
  *-mochitests-1-*.txt.gz) TESTS[1]=mochitest-1;;
  *-mochitests-2-*.txt.gz) TESTS[1]=mochitest-2;;
  *-mochitests-3-*.txt.gz) TESTS[1]=mochitest-3;;
  *-mochitests-4-*.txt.gz) TESTS[1]=mochitest-4;;
  *-mochitests-5-*.txt.gz) TESTS[1]=mochitest-5;;
  *-mochitest-other-*.txt.gz)
    TESTS[1]=mochitest-chrome
    TESTS[2]=mochitest-browser-chrome
    TESTS[3]=mochitest-a11y
    TESTS[4]=mochitest-ipcplugins
    ;;
  *-reftest-*.txt.gz) TESTS[1]=reftest;;
  *-xpcshell-*.txt.gz) TESTS[1]=xpcshell;;
  # It turns out that the only easy way to see the try build is that it logs to
  # try-<platform> instead of try_<platform>. Too bad case can't match [^_]* in
  # regex terms.
  *-linux*.txt.gz|*-macosx*.txt.gz)
    TESTS[1]=check;;
  *.txt.gz)
    echo "Unknown test suite $1"
    TESTS[1]=$1
    ;;
  *)
    echo "Not a .txt.gz file. Probably not correct, bailing" 1>&2
    exit 1;;
esac

GCNO_TAR=$(realpath $2)
# Where intermediate stuff goes, since we unpack a lot of tarballs
OUTPUT_DIRECTORY=/tmp/gcda-unpacked
UNPACK_DIRECTORY=$(mktemp -d)
# The ccov script
CCOV=/src/mozilla-tools/mozilla-coverage/ccov.py
mkdir -p $OUTPUT_DIRECTORY
mkdir -p $UNPACK_DIRECTORY
NUM_FILES=$(gunzip -c $1 | awk '
/~!@!~!@!~!@!~!@!~!@!~!@!~!@!~!@!~/ {
  enable = 1 - enable
  if (enable == 1)
    out_file = out_file + 1
}
/^[A-Za-z0-9+/=]*$/ {
  if (enable)
    print $0 >>"'$UNPACK_DIRECTORY'/results-" out_file ".bin"
}
END {
  print out_file
}')
echo "Found $NUM_FILES test suites in $1"

if [ $NUM_FILES -ne ${#TESTS[*]} ]; then
  echo "Expected ${#TESTS[*]} tests, bailing!" 1>&2
  exit 1
fi

for i in `seq $NUM_FILES`; do
  echo "Unpacking results for ${TESTS[i]}..."
  testdir=$UNPACK_DIRECTORY/unpacked-$i
  logfile=$OUTPUT_DIRECTORY/"${TESTS[i]}".log
  mkdir $testdir
  base64 -d $UNPACK_DIRECTORY/results-$i.bin | tar -xj -C $testdir
  # XXX: This is necessary for lcov/gcov to work properly
  find $testdir -name 'jchuff.gcda' | xargs rm
  
  pushd $testdir &>/dev/null
  while [ $(ls | wc -l) == 1 ]; do
    cd $(ls)
  done
  tar -xjf $GCNO_TAR
  echo "Output from lcov:" >$logfile
  lcov -c -d . -o $UNPACK_DIRECTORY/test-$i-pre.info --gcov-tool=gcov-4.5 \
       -t "${TESTS[i]}" &>>$logfile
  echo "Output from ccov:" >>$logfile
  $CCOV -a $UNPACK_DIRECTORY/test-$i-pre.info -e '/builds/*' \
    -o $OUTPUT_DIRECTORY/"${TESTS[i]}".info &>>$logfile
  popd &>/dev/null
  rm -f $UNPACK_DIRECTORY/results-$i.bin $OUTPUT_DIRECTORY/test-$i-pre.info
  rm -rf $testdir
done
rm -rf $UNPACK_DIRECTORY
