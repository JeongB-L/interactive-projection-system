## Run the virtual touchscreen on the Raspberry Pi

Do every step in a terminal **on the Pi's desktop** (for example over Raspberry Pi Connect's screen sharing), not over plain SSH: `test.py` opens a camera preview window, which needs the display. Use `--headless` if there is no display.

Only one program can use the camera at a time. If the main pipeline is running (`python3 ips.py pi-start`), stop it first:
```bash
cd /home/username/interactive-projection-system
python3 ips.py stop
```

### 1. Check whether the virtual touchscreen is already set up
The kernel module stays loaded until the Pi reboots. Check it first:
```bash
lsmod | grep virtual_touchscreen
ls -l /dev/virtual_touchscreen
grep virtual_touchscreen /proc/devices
```
It is ready when `lsmod` lists `virtual_touchscreen`, and `/dev/virtual_touchscreen` exists as a character device (`crw...`) whose major number (`509` on this Pi) matches the number in `/proc/devices`. If so, skip to step 3.

### 2. Set it up (after a reboot)
The setup script is `~/Desktop/virtual_touchscreen_setup.sh`. Its last line, `nc $1 9494 > /dev/virtual_touchscreen`, receives touch commands **over the network** and needs a sender address (`sudo ~/Desktop/virtual_touchscreen_setup.sh SENDER_IP`). For local use with `test.py`, run only its first two steps by hand:
```bash
cd /home/username/virtual_touchscreen
sudo insmod virtual_touchscreen.ko
grep virtual_touchscreen /proc/devices
sudo mknod /dev/virtual_touchscreen c 509 0
```
Use the major number printed by `grep` in the `mknod` line if it is not `509`. Then repeat the checks from step 1.

Do not run `cat /dev/virtual_touchscreen` to check the device. It does not confirm that the setup worked.

### 3. Activate the Python environment
```bash
cd /home/username/interactive-projection-system
source .venv/bin/activate
cd version1
```

### 4. Run the script
To print the touchscreen commands in the terminal without touching the desktop:
```bash
python test.py
```
To send them to the virtual touchscreen as well (the device is owned by root, so `tee` needs `sudo`):
```bash
python test.py | sudo tee /dev/virtual_touchscreen
```
Hold a thumbs-up (`Thumb_Up`) to press and lower your thumb to release. Add `--debug` to see the detected gesture and score on stderr. Press **Q** or **Esc** in the preview window, or `Ctrl+C` in the terminal, to stop.

`test_click_only.py` is an older variant that sends only touch down/up (no pointer position). Run it the same way.

When you are done, restart the main pipeline if needed with `python3 ips.py pi-start`.
