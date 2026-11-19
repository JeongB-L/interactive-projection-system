## Run the virtual touchscreen on the Raspberry Pi

### 1. Set up the virtual touchscreen
Open a terminal on the Raspberry Pi desktop and start a root shell.
```bash
sudo -i
```
Run the virtual touchscreen setup from the Pi's desktop in the root shell.
```bash
cd ~/Desktop
virtual touchscreen setup filename i don't remember
```
Then confirm that the device is available.
```bash
cat /dev/virtual_touchscreen
```
### 2. Activate the Python environment
In the same terminal, open the project directory, activate the virtual environment, and enter the `version1` folder.
```bash
cd /home/username/interactive_projection_system
source .venv/bin/activate
cd version1
```
### 3. Run the script
To preview the touchscreen commands in the terminal, run the script directly.
```bash
python test.py
```
To display the commands in the terminal and send them to the virtual touchscreen, pipe the output.
```bash
python test.py | tee /dev/virtual_touchscreen
```
Press `Ctrl+C` in the terminal to stop the script.