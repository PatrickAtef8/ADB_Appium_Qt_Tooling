#!/bin/bash
# Run this script: bash diagnose_device.sh 3c444f47

SERIAL=${1:-3c444f47}
echo "============================================"
echo "  Device Diagnostic for: $SERIAL"
echo "============================================"
echo ""

echo "--- Device Info ---"
adb -s $SERIAL shell getprop ro.product.model 2>&1
adb -s $SERIAL shell getprop ro.product.manufacturer 2>&1
adb -s $SERIAL shell getprop ro.build.version.release 2>&1
adb -s $SERIAL shell getprop ro.build.version.sdk 2>&1
adb -s $SERIAL shell getprop ro.product.cpu.abi 2>&1
echo ""

echo "--- ADB Connection Type ---"
adb -s $SERIAL get-state 2>&1
adb -s $SERIAL shell id 2>&1
echo ""

echo "--- screenrecord binary ---"
adb -s $SERIAL shell "ls -la /system/bin/screenrecord 2>&1 || echo MISSING"
adb -s $SERIAL shell "which screenrecord 2>&1 || echo NOT_IN_PATH"
echo ""

echo "--- app_process binary ---"
adb -s $SERIAL shell "ls -la /system/bin/app_process* 2>&1 || echo MISSING"
echo ""

echo "--- Can write to /data/local/tmp? ---"
adb -s $SERIAL shell "echo test > /data/local/tmp/adb_test.txt && echo WRITABLE || echo NOT_WRITABLE"
adb -s $SERIAL shell "rm -f /data/local/tmp/adb_test.txt"
echo ""

echo "--- Available screen capture tools ---"
for bin in screenrecord screencap ffmpeg recordscreen minicap; do
  result=$(adb -s $SERIAL shell "which $bin 2>/dev/null || ls /system/bin/$bin 2>/dev/null || echo MISSING")
  echo "  $bin: $result"
done
echo ""

echo "--- Can push a file? ---"
echo "test" > /tmp/adb_push_test.txt
adb -s $SERIAL push /tmp/adb_push_test.txt /data/local/tmp/adb_push_test.txt 2>&1
adb -s $SERIAL shell "cat /data/local/tmp/adb_push_test.txt && rm /data/local/tmp/adb_push_test.txt"
echo ""

echo "--- adb forward works? ---"
adb -s $SERIAL forward tcp:27183 localabstract:test_socket 2>&1
adb -s $SERIAL forward --remove tcp:27183 2>/dev/null
echo ""

echo "--- Shell permissions ---"
adb -s $SERIAL shell "id; whoami 2>/dev/null; getprop ro.secure; getprop ro.debuggable"
echo ""

echo "============================================"
echo "  DONE — paste the output above to Claude"
echo "============================================"
