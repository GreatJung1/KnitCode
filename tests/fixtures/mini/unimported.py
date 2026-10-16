def caller():
    launch()  # Should NOT resolve to sublist3r.launch: no import.

import subbrute.subbrute

def called_via_package():
    subbrute.subbrute.print_target()
