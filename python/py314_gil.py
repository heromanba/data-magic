import sys
# Returns True if GIL is active, False if running in free-threaded parallel mode
print("Is GIL active?", sys._is_gil_enabled()) 
