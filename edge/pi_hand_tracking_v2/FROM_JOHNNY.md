# Johnny's hand tracker (V2), kept unchanged

Everything in this folder (except this note) is exactly what Johnny Nguyen uploaded as
`pi_hand_tracking_v2.zip`, byte for byte. It still runs on its own:

    cd edge/pi_hand_tracking_v2 && python main.py --backend picamera2 --show

Do not edit these files to integrate them with the hub. The integration lives next to this folder in
`edge/motion_bridge.py`, which loads `tracker.py` (the motion engine) and `main.py` (the latest-frame
camera) from here without changing them. Improvements to the tracker itself belong here, made by Johnny.

His setup and benchmarking instructions are in `README.md` in this folder (originally he had also pasted them at the
bottom of the project README; they were moved here so the main README stays short).
