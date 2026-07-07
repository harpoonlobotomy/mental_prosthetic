import json
import os
import queue
import subprocess
import threading
import time
import tkinter as tk
from datetime import datetime, timedelta
from tkinter import filedialog, messagebox, ttk

CONFIG_FILE = "config.json"


class DriveScannerApp:

    def __init__(self, root:tk.BaseWidget):
        self.root = root
        self.root.title("Multi-Drive File Scanner & Tree Pruner")
        self.root.geometry("1300x750")

        # Fallback Default Preferences
        self.configured_drives = ["C", "D"]
        self.scan_blacklist = ["Windows", ".vscode", "node_modules", "$Recycle.Bin", "Program Files", "Program Files (x86)"]
        self.current_session_path = None  # Track the active JSON file link

        # Re-hydrate layout configurations from local disk storage profile
        self.load_app_preferences()

        # State tracking arrays
        self.all_files = []  # Accumulating list of absolute file paths
        self.excluded_dirs = set()  # Set of normalized LOWERCASE folder paths for backend matching
        self.display_excluded_map = {} # Maps lowercase path -> true original casing path for listbox
        self.scan_queue = queue.Queue()
        self.is_scanning = False


        # Automatically seed the backend exclusion set with your blacklist paths
        self.sync_blacklist_to_exclusions()

        # Main window containers
        self.drive_check_frame = None
        self.drive_checkbox_vars = {}  # Tracks tk.BooleanVar objects for the UI checkboxes
        self.tree_frame = None  # Reference layout frame to support dynamic label titles

        self._build_ui()
        self._setup_polling()

        # Proactively load the last session if one was recorded in preferences
        print("about to load last session")
        self.auto_load_last_session()

        self.scan_cancel_requested = False  # The flag telling the thread to abort immediately
        self.confirming_cancel = False       # Tracks if user is on step 2 of clicking cancel
        # Intercept the Windows 'X' close button to trigger autosave
        self.root.protocol("WM_DELETE_WINDOW", self.on_window_close)

    def load_app_preferences(self):
        """Loads persistent user preferences and the last active session track from disk."""
        print("load app preferences")
        if os.path.exists(CONFIG_FILE):
            try:
                print("config file exists")
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.configured_drives = data.get("drives", ["C", "D"])
                    # FIXED: Added self.scan_blacklist as the safe fallback parameter so it preserves defaults if key is missing!
                    self.scan_blacklist = data.get("blacklist", self.scan_blacklist)
                    self.current_session_path = data.get("last_session_path", None)
            except Exception:
                pass

    def save_app_preferences(self):
        """Saves current drives, blacklist variables, and last active session path to disk."""
        try:
            print(f"Saving preferences now. current session: {self.current_session_path}")
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump({
                    "drives": self.configured_drives,
                    "blacklist": self.scan_blacklist,
                    "last_session_path": self.current_session_path
                }, f, indent=4)
        except Exception as e:
            messagebox.showerror("Error", f"Failed to save system preferences: {str(e)}")

    def sync_blacklist_to_exclusions(self):
        """Pre-seeds scanning filters based on active session files or global drive availability."""
        active_drives = set()

        # Scenario A: If there is data in the cache, pull drives from existing logs
        if self.all_files:
            for file_path in self.all_files:
                drive, _ = os.path.splitdrive(file_path)
                if drive:
                    active_drives.add(drive.upper().replace("\\", ""))
        # Scenario B: If the workspace is empty/cleared, look at all globally configured drive targets
        else:
            active_drives = set(self.configured_drives)

        # Append blacklist folders based strictly on genuine drive/folder existence rules
        for drive in active_drives:
            if not os.path.exists(f"{drive}:\\"):
                continue

            for folder in self.scan_blacklist:
                target_path = os.path.join(f"{drive}:\\", folder)
                norm_lower = os.path.normpath(target_path).lower()

                if os.path.exists(target_path):
                    self.excluded_dirs.add(norm_lower)
                    self.display_excluded_map[norm_lower] = os.path.normpath(target_path)


    def sync_blacklist_to_exclusions2(self):
        """Pre-seeds scanning filters so blacklisted roots show up visually on screen layout."""
        for drive in self.configured_drives:
            for folder in self.scan_blacklist:
                target_path = os.path.join(f"{drive}:\\", folder)
                norm_lower = os.path.normpath(target_path).lower()
                self.excluded_dirs.add(norm_lower)
                self.display_excluded_map[norm_lower] = os.path.normpath(target_path)

    def set_status_text(self, text):
        """Helper to write status updates directly into the main tree panel's frame header."""
        if self.tree_frame:
            self.tree_frame.config(text=f" [{text}] ")


    def _build_ui(self):
        """Creates the layout with unified control bar and a clean trifecta of vertical panels."""
        # Top Control Panel
        control_frame = ttk.LabelFrame(self.root, text=" Session & Scan Controls ")
        control_frame.pack(fill="x", padx=10, pady=5)

        # Space out row controls tightly across the screen width axis grid
        control_frame.columnconfigure(0, weight=0)
        control_frame.columnconfigure(1, weight=0)
        control_frame.columnconfigure(2, weight=0)
        control_frame.columnconfigure(3, weight=0)
        control_frame.columnconfigure(4, weight=0)
        control_frame.columnconfigure(5, weight=0)
        control_frame.columnconfigure(6, weight=0)
        control_frame.columnconfigure(7, weight=0)
        control_frame.columnconfigure(8, weight=1) # Invisible spacer cell pushes Quit to the far right

        # Column 0: Dynamic Drive Checkboxes Container
        self.drive_check_frame = ttk.Frame(control_frame)
        self.drive_check_frame.grid(row=0, column=0, padx=(5, 15), pady=5, sticky="w")
        self.refresh_drive_checkboxes()

        # Column 1 & 2: Extensions Filter Input
        ttk.Label(control_frame, text="Extensions:").grid(row=0, column=1, padx=2, pady=5, sticky="w")
        self.ext_entry = ttk.Entry(control_frame, width=15)
        self.ext_entry.insert(0, ".txt, .py, .md")
        self.ext_entry.grid(row=0, column=2, padx=(2, 15), pady=5, sticky="w")

        # Column 3 & 4: Age Limit Dropdown Selector
        ttk.Label(control_frame, text="Age Limit:").grid(row=0, column=3, padx=2, pady=5, sticky="w")
        self.date_combo = ttk.Combobox(
            control_frame,
            values=["Any Time", "Within 1 Month", "Within 3 Months", "Within 6 Months", "Within 1 Year"],
            width=15,
            state="readonly",
        )
        self.date_combo.set("Within 3 Months")
        self.date_combo.grid(row=0, column=4, padx=(2, 15), pady=5, sticky="w")

        # Core Action Buttons - Enriched with internal padding margins (ipadx) and outer spacing gaps (padx)
        self.scan_btn = ttk.Button(control_frame, text="Start Scan", command=self.start_scan)
        self.scan_btn.grid(row=0, column=5, padx=6, pady=5, ipadx=8)

        self.load_btn = ttk.Button(control_frame, text="Load Session", command=self.load_session)
        self.load_btn.grid(row=0, column=6, padx=6, pady=5, ipadx=8)

        self.save_btn = ttk.Button(control_frame, text="Save Session As...", command=self.save_session)
        self.save_btn.grid(row=0, column=7, padx=6, pady=5, ipadx=8)

        self.clear_btn = ttk.Button(control_frame, text="Clear Workspace", command=self.clear_workspace)
        self.clear_btn.grid(row=0, column=8, padx=6, pady=5, ipadx=8, sticky="w")

        self.settings_btn = ttk.Button(control_frame, text="Settings...", command=self.open_settings_window)
        self.settings_btn.grid(row=0, column=9, padx=6, pady=5, ipadx=8, sticky="e")

        self.abort_btn = ttk.Button(control_frame, text="Quit Without Saving", command=self.quit_without_saving)
        self.abort_btn.grid(row=0, column=10, padx=(15, 6), pady=5, ipadx=8, sticky="e")

        # Main Splitter Layout - Preserves the seamless vertical alignment trifecta of panels
        paned_window = ttk.PanedWindow(self.root, orient="horizontal")
        paned_window.pack(fill="both", expand=True, padx=10, pady=5)

        # PANEL 1: File / Folder Tree Header (Houses our Live Status Text Title Line)
        self.tree_frame = ttk.LabelFrame(paned_window, text=" [Ready] ")
        paned_window.add(self.tree_frame, weight=4)

        self.tree = ttk.Treeview(self.tree_frame, columns=("path"), show="tree")
        self.tree.pack(side="left", fill="both", expand=True, padx=(5, 0), pady=5)

        tree_scroll = ttk.Scrollbar(self.tree_frame, orient="vertical", command=self.tree.yview)
        tree_scroll.pack(side="right", fill="y", pady=5, padx=(0, 5))
        self.tree.configure(yscrollcommand=tree_scroll.set)

        # Bind Tree Triggers
        self.tree.bind("<<TreeviewSelect>>", self.on_tree_select)
        self.tree.bind("<Double-1>", self.on_tree_double_click)
        self.context_menu = tk.Menu(self.root, tearoff=0)
        self.context_menu.add_command(label="Prune/Exclude this Folder", command=self.prune_selected)
        self.tree.bind("<Button-3>", self.show_context_menu)

        # PANEL 2: Central Recursive Contents Preview List (Buffered with activestyle & edge padx)
        glimpse_frame = ttk.LabelFrame(paned_window, text=" Recursive Contents Preview ")
        paned_window.add(glimpse_frame, weight=3)

        preview_split = ttk.PanedWindow(glimpse_frame, orient="vertical")
        preview_split.pack(fill="both", expand=True, padx=5, pady=5)

        #
        # Upper pane - recursive contents list
        #

        list_frame = ttk.Frame(preview_split)
        preview_split.add(list_frame, weight=1)

        self.glimpse_listbox = tk.Listbox(
            list_frame,
            selectmode="single",
            activestyle="none"
        )

        self.glimpse_listbox.pack(side="left", fill="both", expand=True)

        glimpse_scroll = ttk.Scrollbar(
            list_frame,
            orient="vertical",
            command=self.glimpse_listbox.yview
        )
        glimpse_scroll.pack(side="right", fill="y")

        self.glimpse_listbox.configure(yscrollcommand=glimpse_scroll.set)

        self.glimpse_listbox.bind("<Double-1>", self.on_glimpse_double_click)
        self.glimpse_listbox.bind("<<ListboxSelect>>", self.on_glimpse_select)

        #
        # Lower pane - preview
        #

        preview_frame = ttk.Frame(preview_split)
        preview_split.add(preview_frame, weight=1)

        preview_y = ttk.Scrollbar(preview_frame, orient="vertical")
        preview_x = ttk.Scrollbar(preview_frame, orient="horizontal")

        self.preview_text = tk.Text(
            preview_frame,
            wrap="none",
            state="disabled",
            font=("Consolas", 10),
            xscrollcommand=preview_x.set,
            yscrollcommand=preview_y.set
        )

        preview_y.config(command=self.preview_text.yview)
        preview_x.config(command=self.preview_text.xview)

        preview_y.pack(side="right", fill="y")
        preview_x.pack(side="bottom", fill="x")
        self.preview_text.pack(side="left", fill="both", expand=True)

        # Display tabs as approximately two spaces.
        self.preview_text.config(tabs=("16p",))
        # PANEL 3: Right-hand Excluded Folders Storage list (Buffered with activestyle & edge padx)
        exclude_frame = ttk.LabelFrame(paned_window, text=" Excluded Folders ")
        paned_window.add(exclude_frame, weight=2)

        self.exclude_listbox = tk.Listbox(exclude_frame, selectmode="single", activestyle="none")
        self.exclude_listbox.pack(fill="both", expand=True, padx=8, pady=5)
        self.refresh_exclusion_listbox_view()

        remove_exclude_btn = ttk.Button(exclude_frame, text="Restore Selected Folder", command=self.restore_selected)
        remove_exclude_btn.pack(side="bottom", fill="x", padx=5, pady=5)

    def refresh_drive_checkboxes(self):
        """Rebuilds top checkboxes list row configurations based on settings."""
        for widget in self.drive_check_frame.winfo_children():
            widget.destroy()

        old_vars = self.drive_checkbox_vars
        self.drive_checkbox_vars = {}

        for drive in sorted(self.configured_drives):
            is_checked = old_vars[drive].get() if drive in old_vars else False
            var = tk.BooleanVar(value=is_checked)
            self.drive_checkbox_vars[drive] = var

            cb = ttk.Checkbutton(self.drive_check_frame, text=f"{drive}:", variable=var)
            cb.pack(side="left", padx=4)

    def refresh_exclusion_listbox_view(self):
        """Re-populates exclusions panel listing rows with matching native casings."""
        self.exclude_listbox.delete(0, tk.END)
        for lower_key in sorted(self.excluded_dirs):
            true_case_path = self.display_excluded_map.get(lower_key, lower_key)
            self.exclude_listbox.insert(tk.END, true_case_path)
    def start_scan(self):
        """Initialises background scanning or manages the dynamic two-step cancellation sequence."""
        # IF A SCAN IS ALREADY RUNNING
        if self.is_scanning:
            if not self.confirming_cancel:
                # Step 1: Shift button text to double-check their intention
                self.confirming_cancel = True
                self.scan_btn.config(text="Confirm Cancel?")
                self.set_status_text("Click again to confirm scan termination...")
            else:
                # Step 2: User clicked it a second time. Fire the absolute abort signal.
                self.scan_cancel_requested = True
                self.scan_btn.config(state="disabled", text="Stopping...")
                self.set_status_text("Sending termination signal to background scan thread...")
            return

        # STARTING A FRESH SCAN (Normal Behaviour)
        target_drives = [d for d, var in self.drive_checkbox_vars.items() if var.get()]
        if not target_drives:
            messagebox.showerror("Error", "Please check at least one drive box to scan.")
            return

        extensions = [ext.strip().lower() for ext in self.ext_entry.get().split(",")]
        extensions = [ext if ext.startswith(".") else f".{ext}" for ext in extensions if ext]

        if not extensions:
            messagebox.showerror("Error", "Please provide at least one file extension.")
            return

        cutoff_timestamp = self._get_cutoff_timestamp()
        self.glimpse_listbox.delete(0, tk.END)
        self.show_preview_text("")

        # Flip operational trackers
        self.is_scanning = True
        self.scan_cancel_requested = False
        self.confirming_cancel = False

        # Transform button into a live Cancel option and lock out surrounding data toggles
        self.scan_btn.config(text="End Scan")
        self.load_btn.config(state="disabled")
        self.save_btn.config(state="disabled")
        self.clear_btn.config(state="disabled")
        self.settings_btn.config(state="disabled")

        self.set_status_text("Initializing scan thread execution background task...")
        self.root.update_idletasks()

        threading.Thread(
            target=self._bg_scan,
            args=(target_drives, extensions, cutoff_timestamp),
            daemon=True,
        ).start()



    def _get_cutoff_timestamp(self):
        """Returns older epoch constraints based on selector choices."""
        selection = self.date_combo.get()
        now = datetime.now()

        if selection == "Within 1 Month":
            delta = timedelta(days=30)
        elif selection == "Within 3 Months":
            delta = timedelta(days=90)
        elif selection == "Within 6 Months":
            delta = timedelta(days=180)
        elif selection == "Within 1 Year":
            delta = timedelta(days=365)
        else:
            return None

        return (now - delta).timestamp()

    def _bg_scan(self, target_drives, extensions, cutoff_time):
        """High-speed background scanning engine with drive-aware progress and instant exit flags."""
        target_prefixes = [f"{d.upper()}:\\" for d in target_drives]
        self.all_files = [p for p in self.all_files if not any(p.upper().startswith(prefix) for prefix in target_prefixes)]
        normalized_blacklist = {item.strip().lower() for item in self.scan_blacklist if item.strip()}

        for drive in target_drives:
            # INTERCEPT: Drop out instantly if a cancel signal was broadcast
            if self.scan_cancel_requested:
                break

            if not os.path.exists(f"{drive}:\\"):
                continue

            self.scan_queue.put(("progress", f"Scanning {drive}: Drive... Starting search."))

            try:
                root_contents = os.listdir(f"{drive}:\\")
                top_level_folders = [f for f in root_contents if os.path.isdir(os.path.join(f"{drive}:\\", f))]
            except Exception:
                top_level_folders = []

            total_roots = len(top_level_folders)
            current_root_index = 0

            for root_dir, dirs, files in os.walk(f"{drive}:\\"):
                # INTERCEPT: Check inside the nested directory crawls for a mid-drive cancellation request
                if self.scan_cancel_requested:
                    break

                norm_root = os.path.normpath(root_dir).lower()
                path_parts = norm_root.split(os.sep)

                if any(blacklisted_word in path_parts for blacklisted_word in normalized_blacklist):
                    dirs[:] = []  # Tell os.walk to completely abandon this entire branch layout
                    continue

                """if any(part in normalized_blacklist for part in path_parts):
                    dirs[:] = []
                    continue"""

                if os.path.dirname(os.path.normpath(root_dir)) == f"{drive}:\\":
                    current_root_index += 1
                    folder_name = os.path.basename(root_dir)
                    self.scan_queue.put((
                        "progress",
                        f"Scanning {drive}: Drive — Folder {current_root_index} of {total_roots} ({folder_name})"
                    ))

                for file in files:
                    # INTERCEPT: Final granular deep-nested loop escape checkpoint
                    if self.scan_cancel_requested:
                        break

                    if any(file.lower().endswith(ext) for ext in extensions):
                        full_path = os.path.join(root_dir, file)
                        try:
                            mtime = os.path.getmtime(full_path)
                            if cutoff_time and mtime < cutoff_time:
                                continue
                            self.scan_queue.put(("file", full_path))
                        except (PermissionError, FileNotFoundError):
                            continue

        # Broadcast termination payload
        if self.scan_cancel_requested:
            self.scan_queue.put(("cancelled", None))
        else:
            self.scan_queue.put(("done", None))


    def _bg_scan2(self, target_drives, extensions, cutoff_time):
        """High-speed background scanning engine with drive-aware progress updates."""
        target_prefixes = [f"{d.upper()}:\\" for d in target_drives]
        self.all_files = [p for p in self.all_files if not any(p.upper().startswith(prefix) for prefix in target_prefixes)]
        normalized_blacklist = {item.strip().lower() for item in self.scan_blacklist if item.strip()}

        for drive in target_drives:
            if not os.path.exists(f"{drive}:\\"):
                continue

            # BROADCAST SWITCH: Let the main thread know we have officially shifted to a new drive letter target
            self.scan_queue.put(("progress", f"Scanning {drive}: Drive... Starting search."))

            try:
                root_contents = os.listdir(f"{drive}:\\")
                top_level_folders = [f for f in root_contents if os.path.isdir(os.path.join(f"{drive}:\\", f))]
            except Exception:
                top_level_folders = []

            total_roots = len(top_level_folders)
            current_root_index = 0

            for root_dir, dirs, files in os.walk(f"{drive}:\\"):
                norm_root = os.path.normpath(root_dir).lower()
                path_parts = norm_root.split(os.sep)

                # Skip blacklisted folder trees instantly
                if any(part in normalized_blacklist for part in path_parts):
                    dirs[:] = []
                    continue

                if os.path.dirname(os.path.normpath(root_dir)) == f"{drive}:\\":
                    current_root_index += 1
                    folder_name = os.path.basename(root_dir)
                    self.scan_queue.put((
                        "progress",
                        f"Scanning {drive}: Drive — Folder {current_root_index} of {total_roots} ({folder_name})"
                    ))

                for file in files:
                    if any(file.lower().endswith(ext) for ext in extensions):
                        full_path = os.path.join(root_dir, file)
                        try:
                            mtime = os.path.getmtime(full_path)
                            if cutoff_time and mtime < cutoff_time:
                                continue
                            self.scan_queue.put(("file", full_path))
                        except (PermissionError, FileNotFoundError):
                            continue

        self.scan_queue.put(("done", None))


    def _bg_scan2(self, target_drives, extensions, cutoff_time):
        """Background scanning task loop worker thread. Protected against broken links."""
        target_prefixes = [f"{d.upper()}:\\" for d in target_drives]
        self.all_files = [p for p in self.all_files if not any(p.upper().startswith(prefix) for prefix in target_prefixes)]
        normalized_blacklist = {item.strip().lower() for item in self.scan_blacklist if item.strip()}

        for drive in target_drives:
            start_dir = f"{drive}:\\"
            if not os.path.exists(start_dir):
                continue

            for root_dir, dirs, files in os.walk(start_dir):
                # FIXED: Protect scan engine thread by skipping system locked partitions securely
                try:
                    dirs[:] = [d for d in dirs if d.lower() not in normalized_blacklist]
                except (PermissionError, FileNotFoundError):
                    dirs[:] = []
                    continue

                for file in files:
                    if any(file.lower().endswith(ext) for ext in extensions):
                        full_path = os.path.join(root_dir, file)
                        try:
                            mtime = os.path.getmtime(full_path)
                            if cutoff_time and mtime < cutoff_time:
                                continue
                            self.scan_queue.put(("file", full_path))
                        except (PermissionError, FileNotFoundError):
                            continue

        self.scan_queue.put(("done", None))


    def _setup_polling(self):
        """Periodically checks the queue for files and live heartbeat folder ticks."""
        def poll():
            try:
                while True:
                    msg_type, data = self.scan_queue.get_nowait()
                    if msg_type == "file":
                        self.all_files.append(data)
                    elif msg_type == "progress":
                        self.set_status_text(f"{data} | Found: {len(self.all_files)} files")
                    elif msg_type == "cancelled":
                        # HANDLE CANCELLATION: Re-enable the interface and restore the baseline workspace elements
                        self.is_scanning = False
                        self.confirming_cancel = False
                        self.scan_cancel_requested = False

                        self.scan_btn.config(state="normal", text="Execute Scan")
                        self.load_btn.config(state="normal")
                        self.save_btn.config(state="normal")
                        self.clear_btn.config(state="normal")
                        self.settings_btn.config(state="normal")

                        self.set_status_text("Scan aborted by user. Workspace cache preserved.")
                        self.update_tree_view_with_feedback()
                    elif msg_type == "done":
                        self.is_scanning = False
                        self.confirming_cancel = False
                        self.scan_cancel_requested = False

                        # Return button shapes back to standard layouts
                        self.scan_btn.config(state="normal", text="Execute Scan")
                        self.load_btn.config(state="normal")
                        self.save_btn.config(state="normal")
                        self.clear_btn.config(state="normal")
                        self.settings_btn.config(state="normal")

                        self.set_status_text("Scan complete! Compiling visual tree overlay, please wait...")
                        self.sync_blacklist_to_exclusions()
                        self.refresh_exclusion_listbox_view()
                        self.root.after(100, self.update_tree_view_with_feedback)
                    self.scan_queue.task_done()
            except queue.Empty:
                pass
            self.root.after(100, poll)

        self.root.after(100, poll)


    def _setup_polling2(self):
        """Periodically checks the queue for files found by the background thread."""
        def poll():
            try:
                while True:
                    msg_type, data = self.scan_queue.get_nowait()
                    if msg_type == "file":
                        self.all_files.append(data)
                        self.set_status_text(f"Discovered {len(self.all_files)} total files...")
                    elif msg_type == "done":
                        self.is_scanning = False
                        self.scan_btn.config(state="normal")
                        self.set_status_text("Scan complete! Compiling visual tree overlay, please wait...")

                        # Apply the blacklist rules specifically to the newly fetched files list right now
                        self.sync_blacklist_to_exclusions()
                        self.refresh_exclusion_listbox_view()

                        # Pass off to the buffered thread safety loop handler
                        self.root.after(100, self.update_tree_view_with_feedback)

                    self.scan_queue.task_done()
            except queue.Empty:
                pass
            self.root.after(100, poll)

        self.root.after(100, poll)

    def update_tree_view_with_feedback(self):
        """Wraps the core tree view generator to guarantee a smooth interface redraw."""
        # Force the UI thread to clear any pending layout processes first
        self.root.update_idletasks()

        # Execute the tree nodes compilation pass
        self.update_tree_view()

        # Flash the success message right into the tree header title zone
        self.set_status_text(f"Active workspace total: {len(self.all_files)} files.")


    def open_settings_window(self):
        """Creates a standalone preferences config window modal window layer."""
        settings_win = tk.Toplevel(self.root)
        settings_win.title("Workspace Settings")
        settings_win.geometry("500x400")
        settings_win.transient(self.root)
        settings_win.grab_set()

        ttk.Label(settings_win, text="Available Drive Letters (comma-separated):", font=("", 10, "bold")).pack(anchor="w", padx=10, pady=5)
        drive_entry = ttk.Entry(settings_win, width=50)
        drive_entry.insert(0, ", ".join(self.configured_drives))
        drive_entry.pack(fill="x", padx=10, pady=2)

        ttk.Label(settings_win, text="Folder Name Scan Blacklist (comma-separated):", font=("", 10, "bold")).pack(anchor="w", padx=10, pady=10)
        blacklist_text = tk.Text(settings_win, width=50, height=8, wrap="word")
        blacklist_text.insert("1.0", ", ".join(self.scan_blacklist))
        blacklist_text.pack(fill="both", expand=True, padx=10, pady=2)

        def save_settings():
            drives = [d.strip().upper() for d in drive_entry.get().split(",") if d.strip()]
            valid_drives = [d for d in drives if len(d) == 1 and d.isalpha()]
            blacklist_items = [b.strip() for b in blacklist_text.get("1.0", "end").split(",") if b.strip()]

            self.configured_drives = valid_drives
            self.scan_blacklist = blacklist_items

            self.save_app_preferences()

            # Wipes out transient user choices to clean-rehydrate newly submitted blacklist constraints safely
            self.excluded_dirs.clear()
            self.display_excluded_map.clear()
            self.sync_blacklist_to_exclusions()

            self.refresh_drive_checkboxes()
            self.refresh_exclusion_listbox_view()
            self.update_tree_view()

            settings_win.destroy()
            self.set_status_text("System preferences stored to config file.")

        btn_frame = ttk.Frame(settings_win)
        btn_frame.pack(fill="x", side="bottom", pady=10)
        ttk.Button(btn_frame, text="Save Preferences", command=save_settings).pack(side="right", padx=10)
        ttk.Button(btn_frame, text="Cancel", command=settings_win.destroy).pack(side="right")

    def auto_load_last_session(self):
        """Automatically hydrates the workspace on startup if a valid last session exists."""
        if self.current_session_path:
            print("self.current_session_path")
            if os.path.exists(self.current_session_path):
                try:
                    print("self.current_session_path exists")
                    with open(self.current_session_path, "r", encoding="utf-8") as f:
                        session_data = json.load(f)

                    self.all_files = session_data.get("files", [])
                    raw_exclusions = session_data.get("exclusions", [])

                    self.excluded_dirs = set()
                    self.display_excluded_map = {}
                    for path in raw_exclusions:
                        norm_p = os.path.normpath(path)
                        self.excluded_dirs.add(norm_p.lower())
                        self.display_excluded_map[norm_p.lower()] = norm_p

                    self.sync_blacklist_to_exclusions()
                    self.refresh_exclusion_listbox_view()
                    self.update_tree_view()
                    self.set_status_text(f"Auto-loaded: {os.path.basename(self.current_session_path)}")
                except Exception:
                    self.current_session_path = None
                    self.set_status_text("Notice: Last session file was corrupted. Loaded blank window.")
            else:
                filename = os.path.basename(self.current_session_path)
                self.current_session_path = None
                self.save_app_preferences()
                self.set_status_text(f"Notice: Session file '{filename}' was not found. Loaded blank window.")
        else:
            print("No self.current_session_path")

    def update_tree_view(self):
        """Rebuilds the tree control visual overlay while maintaining folder expanded states."""
        expanded_paths = set()
        for item_id in self.tree.get_children():
            stack = [item_id]
            while stack:
                curr_id = stack.pop()
                if self.tree.item(curr_id, "open"):
                    real_path = self.tree.set(curr_id, "path")
                    if real_path:
                        expanded_paths.add(real_path.lower())
                stack.extend(self.tree.get_children(curr_id))

        self.tree.delete(*self.tree.get_children())
        self.glimpse_listbox.delete(0, tk.END)
        self.show_preview_text("")

        node_map = {}

        for file_path in sorted(self.all_files):
            if self._is_excluded(file_path):
                continue

            drive, path_tail = os.path.splitdrive(file_path)
            parts = [drive + os.sep] + [p for p in path_tail.split(os.sep) if p]
            parent_id = ""
            current_path = ""

            for i, part in enumerate(parts):
                if i == 0:
                    current_path = part
                else:
                    current_path = os.path.join(current_path, part)

                node_key = current_path.lower()

                if node_key not in node_map:
                    display_text = part if part else drive
                    should_open = node_key in expanded_paths
                    node_id = self.tree.insert(parent_id, "end", text=display_text, open=should_open)
                    self.tree.set(node_id, "path", current_path)
                    node_map[node_key] = node_id
                else:
                    node_id = node_map[node_key]

                parent_id = node_id

        self.node_map = node_map

    def _is_excluded(self, path):
        """Checks if a path falls inside an active visual exclusion zone."""
        norm_path = os.path.normpath(path).lower()
        for excluded in self.excluded_dirs:
            if norm_path == excluded or norm_path.startswith(excluded + os.sep):
                return True
        return False

    def on_tree_select(self, event):
        """Triggered whenever a user selects a folder node in the tree."""
        selected_items = self.tree.selection()
        if not selected_items:
            return

        target_path = self.tree.set(selected_items[0], "path")
        self.glimpse_listbox.delete(0, tk.END)
        self.show_preview_text("")

        if os.path.isfile(target_path):
            target_path = os.path.dirname(target_path)

        norm_target = os.path.normpath(target_path).lower()
        count = 0

        for file_path in sorted(self.all_files):
            if self._is_excluded(file_path):
                continue

            norm_file_dir = os.path.normpath(os.path.dirname(file_path)).lower()

            if norm_file_dir == norm_target or norm_file_dir.startswith(norm_target + os.sep):
                self.glimpse_listbox.insert(tk.END, file_path)
                count += 1
                if count >= 100:
                    self.glimpse_listbox.insert(tk.END, "... [Truncated: Screen Limit Reached] ...")
                    break

    def on_tree_double_click(self, event):
        """Triggered when double-clicking a file inside the left-hand tree view."""
        selected_items = self.tree.selection()
        if not selected_items:
            return

        target_path = self.tree.set(selected_items[0], "path")
        if os.path.isfile(target_path) and os.path.exists(target_path):
            try:
                subprocess.run(['explorer.exe', '/select,', os.path.normpath(target_path)])
            except Exception as e:
                messagebox.showerror("Error", f"Could not open Windows Explorer: {str(e)}")

    def show_preview_text(self, text):
        """Displays text inside the read-only preview pane."""

        self.preview_text.config(state="normal")
        self.preview_text.delete("1.0", tk.END)
        self.preview_text.insert("1.0", text)
        self.preview_text.config(state="disabled")

        self.preview_text.yview_moveto(0)
        self.preview_text.xview_moveto(0)

    def on_glimpse_select(self, event):
        """Displays the selected file inside the preview pane."""

        selection = self.glimpse_listbox.curselection()
        if not selection:
            return

        path = self.glimpse_listbox.get(selection[0])

        if not os.path.exists(path):
            self.show_preview_text(f"\nNo preview for\n\n{os.path.basename(path)}")
            return

        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()

            self.show_preview_text(text)

        except Exception:
            self.show_preview_text(f"\nNo preview for\n\n{os.path.basename(path)}")


    def on_glimpse_double_click(self, event):
        selection = self.glimpse_listbox.curselection()
        if not selection:
            return

        chosen_path = self.glimpse_listbox.get(selection[0])
        if os.path.exists(chosen_path):
            try:
                subprocess.run(['explorer.exe', '/select,', os.path.normpath(chosen_path)])
            except Exception as e:
                messagebox.showerror("Error", f"Could not open Windows Explorer: {str(e)}")
        else:
            messagebox.showwarning("File Not Found", "This file no longer exists at this location.")

    def show_context_menu(self, event):
        item = self.tree.identify_row(event.y)
        if item:
            self.tree.selection_set(item)
            self.context_menu.post(event.x_root, event.y_root)

    def prune_selected(self):
        selected_item = self.tree.selection()
        if not selected_item:
            return

        target_path = self.tree.set(selected_item[0], "path")
        if os.path.isfile(target_path):
            target_path = os.path.dirname(target_path)

        norm_exclude = os.path.normpath(target_path)
        lower_exclude = norm_exclude.lower()

        if lower_exclude not in self.excluded_dirs:
            self.excluded_dirs.add(lower_exclude)
            self.display_excluded_map[lower_exclude] = norm_exclude
            self.refresh_exclusion_listbox_view()
            self.update_tree_view()

    def restore_selected(self):
        selection = self.exclude_listbox.curselection()
        if not selection:
            return

        removed_path = self.exclude_listbox.get(selection[0])
        lower_remove = os.path.normpath(removed_path).lower()

        if lower_remove in self.excluded_dirs:
            self.excluded_dirs.remove(lower_remove)
            self.display_excluded_map.pop(lower_remove, None)
            self.refresh_exclusion_listbox_view()
            self.update_tree_view()


    def clear_workspace(self):
        """Purges cached file logs but restores baseline config blacklist rules immediately."""
        if self.is_scanning:
            return

        if messagebox.askyesno("Clear Workspace", "Are you sure you want to completely clear all session file data?"):
            # Step 1: Wipe the active session cache completely
            self.all_files = []
            self.excluded_dirs.clear()
            self.display_excluded_map.clear()

            # Step 2: Wiping out all active visual screen data arrays
            self.glimpse_listbox.delete(0, tk.END)
            self.show_preview_text("")
            self.tree.delete(*self.tree.get_children())

            # Step 3: Immediately seed the baseline config blacklist items back into the view models
            # This ensures your text preferences and default strings are preserved on-screen
            self.sync_blacklist_to_exclusions()
            self.refresh_exclusion_listbox_view()

            # Step 4: Detach current session log strings from your permanent configuration tracking
            self.current_session_path = None
            self.save_app_preferences()

            self.set_status_text("Session logs cleared. Baseline config blacklist preferences restored.")

    def save_session(self):
        if not self.all_files:
            messagebox.showwarning("Warning", "There is no scan data to save yet!")
            return

        file_path = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("JSON Files", "*.json")], title="Save Scan Session")
        if not file_path:
            return

        self.current_session_path = file_path
        self.save_app_preferences()
        self._write_session_to_disk(file_path)

    def load_session(self):
        if self.is_scanning:
            return

        file_path = filedialog.askopenfilename(filetypes=[("JSON Files", "*.json")], title="Load Scan Session")
        if not file_path:
            return

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                session_data = json.load(f)

            self.all_files = session_data.get("files", [])
            raw_exclusions = session_data.get("exclusions", [])

            self.excluded_dirs = set()
            self.display_excluded_map = {}
            for path in raw_exclusions:
                norm_p = os.path.normpath(path)
                self.excluded_dirs.add(norm_p.lower())
                self.display_excluded_map[norm_p.lower()] = norm_p

            self.sync_blacklist_to_exclusions()
            self.refresh_exclusion_listbox_view()
            self.current_session_path = file_path
            self.save_app_preferences()
            self.update_tree_view()
            self.set_status_text(f"Session loaded! Active File: {os.path.basename(file_path)}")

        except Exception as e:
            messagebox.showerror("Error", f"Failed to read session file: {str(e)}")

    def _write_session_to_disk(self, file_path):
        """Helper to write the current workspace payload to a specific JSON file."""
        session_data = {
            "files": self.all_files,
            "exclusions": list(self.display_excluded_map.values())
        }
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(session_data, f, indent=4)
            self.set_status_text(f"Updated: {os.path.basename(file_path)}")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to save data: {str(e)}")

    def quit_without_saving(self):
        """Closes the app immediately, bypassing the autosave logic completely."""
        self.root.destroy()

    def on_window_close(self):
        """Triggered automatically if you press the standard Windows close 'X' button."""
        if self.current_session_path and self.all_files:
            self._write_session_to_disk(self.current_session_path)
            self.root.destroy()
        elif self.all_files:
            response = messagebox.askyesnocancel(
                "Unsaved Workspace",
                "You have active scan data. Save before closing?"
            )
            if response is True:
                self.save_session()
                if self.current_session_path:
                    self.root.destroy()
            elif response is False:
                self.root.destroy()
        else:
            self.root.destroy()

if __name__ == "__main__":
    root = tk.Tk()
    app = DriveScannerApp(root)
    root.mainloop()
