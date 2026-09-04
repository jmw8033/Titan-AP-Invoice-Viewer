from tkinter import ttk, messagebox, scrolledtext, font, simpledialog
from weakref import ref
import winsound
from tkcalendar import DateEntry
from collections import defaultdict
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
import tkinter as tk
import os, pymssql, time, threading, re, queue, json, gc, csv

INVOICE_DIR = r"S:\Titan_DM\Titan_Filing\AP_Invoices"
LOG_PATH    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "usage_log.csv")
invoice_re = re.compile(r'(\d+)')

class InvoiceViewer(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Titan AP Invoice Viewer")
        self.iconbitmap(default="icon.ico")
        self.style = ttk.Style(self)
        self.style.theme_use("clam")
        self.tk.call("tk", "scaling", 1.33)
        self.gui_queue = queue.Queue()
        self.geometry("1400x700")

        self.invoices = {} # List of dictionaries, {"VendorID", "InvoiceNum", "InvoiceDate", "ExtAmount", "Filepath"}
        self.company_ids = set() # Set of company IDs for quick lookup
        self.sort_col = "Date"
        self.sort_desc = True # descending
        self.current_rows = []
        self.result_count = 0
        self.displayed_count = 0
        self.page_size = 500
        self._loading_page = False
        self.current_account_filter = ""
        self.broken_companies = []
        self.broken_invoices = []
        self.by_vendor_invoice = []
        self.checks_by_vendor_invoice = defaultdict(list)
        self.accounts_by_vendor_invoice = defaultdict(list)
        self.account_description_by_account = {}
        self.missing_invoices = []
        self.ap_by_record_num = {}
        self.cd_by_check_id = {}
        self.check_record_ids_by_ap_record = defaultdict(list)
        self.check_ids_by_ap_record = defaultdict(list)
        self.duplicate_invoices = []

        self.log_usage()

        # Ignore list for private vendors
        self.ignoring = True
        self.ignore_list = set()
        with open("ignore.json", "r") as f:
            self.ignore_list = set(json.load(f))
        self.protocol("WM_DELETE_WINDOW", self.on_exit)  # runs exit protocol on window closed

        # Loading info
        self.startup_sound()
        self.create_loading_screen()

        # Get data
        self.after(0, lambda: threading.Thread(target=self.load_data, daemon=True).start())
        self.loading_loop_id = self.after(50, self.loading_loop)


    def startup_sound(self):
        winsound.PlaySound("owin31", winsound.SND_ALIAS | winsound.SND_ASYNC)


    def create_loading_screen(self):
        self.loading_bg = tk.PhotoImage(file="logo.png")
        self.loading_canvas = tk.Canvas(self, bg="white", width=1220, height=700)
        self.loading_canvas.pack(expand=True, fill="both", side="top", anchor="w")
        self.loading_canvas.background = self.loading_bg
        self.loading_canvas.create_image(1220/2, 0, anchor="n", image=self.loading_bg)
        
        tk.Label(self.loading_canvas, text="Welcome to Titan AP Invoice Viewer", font=("TKDefaultFont", 24, "bold"), bg="white").pack(side="top", anchor="w")
        tk.Label(self.loading_canvas, text="Please press the Help button for more information about this program", font=("TKDefaultFont", 20), bg="white").pack(side="top", anchor="w")
        tk.Label(self.loading_canvas, text="Loading Titan AP invoices, Please wait...", font=("TKDefaultFont", 16), bg="white").pack(side="top", anchor="w")


    def loading_update(self, msg, color="#000000"):
        #print(msg)
        self.gui_queue.put((msg, color))


    def loading_loop(self):
        try:
            while True:
                msg, color = self.gui_queue.get_nowait()
                tk.Label(self.loading_canvas, text=msg, font=("TKDefaultFont", 16), fg=color, bg="white").pack(side="top", anchor="w")
        except queue.Empty:
            pass
        self.loading_loop_id = self.after(50, self.loading_loop)


    def load_data(self):
        self.t0 = time.perf_counter()

        with ThreadPoolExecutor(max_workers=2) as pool:
            _ = pool.submit(self.load_database)
            file_index = pool.submit(self.load_files)

            file_index = file_index.result()
            _ = _.result()

        # Match files with invoices
        t0 = time.perf_counter()
        for row in self.invoices:
            try:
                row["Filepath"] = file_index.pop((row["VendorID"], row["InvoiceNum"]))
            except:
                self.missing_invoices.append((row["VendorID"], row["InvoiceNum"], row["InvoiceDate"].strftime("%m-%d-%Y")))
        t1 = time.perf_counter()
        self.loading_update(f"Invoice files loaded in {t1 - t0:.2f} seconds.")
        self.loading_update((f"{len(self.broken_companies)} broken titan entries found."), color="#FF0000")
        self.loading_update(f"{len(self.missing_invoices)} missing invoice files.", color="#FF0000")

        # Check for errors
        if len(file_index) > 0:
            for file in file_index:
                self.broken_invoices.append(file_index[file])
            self.loading_update(f"{len(file_index)} invoice files without matches.", color="#FF0000")

        self.after(100, self.load_gui)

    
    def load_gui(self):
        # Destroy loading screen
        self.after_cancel(self.loading_loop_id)
        self.loading_canvas.destroy()

        # Create Frames
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        self.create_treeview()
        self.create_filter_frame()
        self.create_summary_bar()
        self.error_popup = ErrorPopup(self, self.broken_companies, self.broken_invoices, self.missing_invoices, self.duplicate_invoices)
        self.help_popup = HelpPopup(self)

        # Ignore list image
        self.ignore_photo = tk.PhotoImage(file="leaf.png")
        self.ignore_label = tk.Label(self.filter_frame, image=self.ignore_photo)
        self.bind("<Control-F9>", self.add_ignore)
        self.bind("<Control-F10>", self.toggle_ignore_list)

        self.bind("<Control-F9>", self.add_ignore)
        self.bind("<Control-F10>", self.toggle_ignore_list)

        # Restore saved filters
        if hasattr(self, "saved_filters") and self.saved_filters:
            # Dropdowns & Checkboxes
            self.date_filter_var.set(self.saved_filters["date_filter"])
            self.plant_var.set(self.saved_filters["plant"])
            self.all_companies.set(self.saved_filters["all_companies"])
            self.search_names.set(self.saved_filters["search_names"])
            self.pdf_only.set(self.saved_filters["pdf_only"])
            
            # Dates
            self.start_entry.set_date(self.saved_filters["start_date"])
            self.end_entry.set_date(self.saved_filters["end_date"])
            
            # Text Variables
            self.invoice_text.set(self.saved_filters["invoice"])
            self.account_text.set(self.saved_filters["account"])

            # Sorting state
            self.sort_col = self.saved_filters.get("sort_col", "Date")
            self.sort_desc = self.saved_filters.get("sort_desc", True)
            
            # Main Search Bar and Trigger Search
            if hasattr(self, "company_entry"):
                # Remove active trace to stop premature searches
                self.company_entry.company.trace_remove("write", self.company_entry.text_trace)
                
                # Insert saved text
                self.company_entry.insert(0, self.saved_filters["search_target"])
                
                # Apply correct trace based on All Companies state
                if self.all_companies.get():
                    self.company_entry.text_trace = self.company_entry.company.trace_add(
                        "write", lambda *_: self.company_entry.debounced_select(source="company")
                    )
                else:
                    self.company_entry.text_trace = self.company_entry.company.trace_add(
                        "write", self.company_entry.show_suggestions
                    )
                
                # Trigger search manually
                self.company_entry.on_select()
                
            self.saved_filters = None


    def log_usage(self):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        username  = os.environ.get("USERNAME",     "unknown")
        computer  = os.environ.get("COMPUTERNAME", "unknown")

        for attempt in range(5):
            try:
                is_new = not os.path.exists(LOG_PATH) or os.path.getsize(LOG_PATH) == 0
                with open(LOG_PATH, "a", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    if is_new:
                        writer.writerow(["Date/Time", "Username", "Computer"])
                    writer.writerow([timestamp, username, computer])
                return
            except PermissionError:
                # Another machine is writing at the same moment — wait and retry
                time.sleep(0.1 * (attempt + 1))
            except Exception:
                return  # Don't crash the program over a log write


    def load_database(self):
        def load_header():
            # Connect to the database and fetch invoice data
            t0 = time.perf_counter()
            conn = pymssql.connect(
                server="ACAPP1",
                user="titan",
                password="titan",
                database="titan",
            )

            with conn.cursor(as_dict=True) as cur:
                cur.execute("""
                    SELECT APH.VendorID, APH.InvoiceNum, APH.InvoiceDate, APH.Subtotal, APH.Payments, APH.PlantID, APH.RecordNum, V.CompanyName
                    FROM AP_Header APH
                    JOIN Vendors V ON APH.VendorID = V.VendorId
                """)
                data = cur.fetchall()

            self.invoices = [row for row in data if row["VendorID"] and row["InvoiceNum"] and row["InvoiceDate"] 
                            and row["Subtotal"] is not None and row["Payments"] is not None]
            # Check for duplicate invoices
            seen_invoices = set()
            for row in self.invoices:
                key = (row["VendorID"], row["InvoiceNum"])
                if key in seen_invoices:
                    # Format a clean string to show in the error menu
                    self.duplicate_invoices.append(f"{row['VendorID']} - {row['InvoiceNum']} (Record: {row.get('RecordNum', 'N/A')})")
                else:
                    seen_invoices.add(key)
            self.broken_companies = [row for row in data if not (row["VendorID"] and row["InvoiceNum"] and row["InvoiceDate"] 
                                     and row["Subtotal"] is not None and row["Payments"] is not None)]
            self.company_ids = {(row["VendorID"], row["CompanyName"], row["VendorID"] in self.ignore_list) for row in self.invoices if row["VendorID"] and row["CompanyName"]}
            self.by_vendor_invoice = {(row["VendorID"], row["InvoiceNum"]): row for row in self.invoices}

            t1 = time.perf_counter()
            self.loading_update(f"Invoice data loaded in {t1 - t0:.2f} seconds.")
            conn.close()

        def load_checks():
            t0 = time.perf_counter()
            # Connect to the database and fetch check data
            conn = pymssql.connect(server="ACAPP1", user="titan", password="titan", database="titan")

            with conn.cursor(as_dict=True) as cur:
                # --- UPDATE: ADDED CH.CheckID TO SELECT ---
                cur.execute("""
                    SELECT CH.CheckNum, CH.CheckDate, CD.InvoiceNum, CD.Amount, CH.VendorID, CD.RecordID, CD.AP_Record, CH.CheckID
                    FROM Check_Header CH
                    JOIN Check_Detail CD ON CH.CheckID = CD.CheckID
                """)
                data=cur.fetchall()
            for row in data:
                self.checks_by_vendor_invoice[(row["VendorID"], row["InvoiceNum"])].append((row["CheckNum"], row["CheckDate"], row["Amount"]))
                
                ap_rec = row.get("AP_Record")
                rec_id = row.get("RecordID")
                chk_id = row.get("CheckID")
                
                if ap_rec:
                    # Append RecordID
                    if rec_id and rec_id not in self.check_record_ids_by_ap_record[ap_rec]:
                        self.check_record_ids_by_ap_record[ap_rec].append(rec_id)
                    # Append CheckID
                    if chk_id and chk_id not in self.check_ids_by_ap_record[ap_rec]:
                        self.check_ids_by_ap_record[ap_rec].append(chk_id)

            conn.close()
            t1 = time.perf_counter()
            self.loading_update(f"Check data loaded in {t1 - t0:.2f} seconds.")

        def load_accounts():
            t0 = time.perf_counter()
            # Connect to the database and fetch check data
            conn = pymssql.connect(server="ACAPP1", user="titan", password="titan", database="titan")

            with conn.cursor(as_dict=True) as cur:
                cur.execute("""
                    SELECT APH.VendorID, APH.InvoiceNum, APD.Account, APD.ExtAmount, GL.AccountDescription
                    FROM AP_Header APH
                    JOIN AP_Detail APD ON APH.RecordNum = APD.RecordNum
                    JOIN GL_Accounts GL ON APD.Account = GL.AccountNumber
                """)
                data=cur.fetchall()
            for row in data:
                self.accounts_by_vendor_invoice[(row["VendorID"], row["InvoiceNum"])].append((row["Account"], row["ExtAmount"]))
                self.account_description_by_account[row["Account"]] = row["AccountDescription"]

            conn.close()
            t1 = time.perf_counter()
            self.loading_update(f"Check data loaded in {t1 - t0:.2f} seconds.")

        def load_journals():
            t0 = time.perf_counter()
            conn = pymssql.connect(server="ACAPP1", user="titan", password="titan", database="titan")
            with conn.cursor(as_dict=True) as cur:
                cur.execute("""
                    SELECT JournalID, SourceRecordID
                    FROM GL_Journal_Detail_Source
                """)
                data = cur.fetchall()

            for row in data:
                jid = row["JournalID"]
                # Cast to int to perfectly match RecordNum and CheckID
                try:
                    rec_id = int(row["SourceRecordID"])
                except (ValueError, TypeError):
                    rec_id = row["SourceRecordID"] # Fallback if data is unexpectedly non-numeric
                
                if jid.startswith("AP"):
                    self.ap_by_record_num[rec_id] = jid
                elif jid.startswith("CD"):
                    self.cd_by_check_id[rec_id] = jid

            conn.close()
            t1 = time.perf_counter()
            self.loading_update(f"Journal data loaded in {t1 - t0:.2f} seconds.")
        
        with ThreadPoolExecutor(max_workers=4) as pool:
            _ = pool.submit(load_header).result()
            _ = pool.submit(load_checks).result()
            _ = pool.submit(load_accounts).result()
            _ = pool.submit(load_journals).result()
        t1 = time.perf_counter()
        self.loading_update(f"Database loaded in {t1 - self.t0:.2f} seconds.")


    def load_files(self):
        # Scan the invoice directory and create file lookup table
        t0 = time.perf_counter()
        file_index = {}
        for file in os.scandir(INVOICE_DIR):
            file = file.name
            fname = file.split("_") # COMPANY_INVOICE_MM-DD-YYYY_RANDOMINT
            if fname and len(fname) >= 3:
                if fname[0] == "PUCA": # Special case, they have underscore in company ID
                    fname[0] = "PUCA_150"
                    fname[1] = fname[2] # Invoice number

                fname[1] = fname[1].replace("[slash]", "/").replace("[quote]", '"')
                file_index[(fname[0], fname[1])] = os.path.join(INVOICE_DIR,  file)

        t1 = time.perf_counter()
        self.loading_update(f"Invoice files scanned in {t1 - t0:.2f} seconds.")
        return file_index


    def create_filter_frame(self):
        self.filter_frame = ttk.Frame(self, height=60)
        self.filter_frame.grid(row=0, column=0, sticky="ew")
        self.filter_frame.grid_propagate(False)

        # Row 0

        # Company Search bar
        ttk.Label(self.filter_frame, text="Company ID:").grid(row=0, column=0, padx=5)
        self.company_entry = AutoCompleteEntry(self)
        self.company_entry.grid(row=0, column=1, padx=5, pady=5)
        
        # Date Filter Dropdown
        ttk.Label(self.filter_frame, text="Date Filter:").grid(row=0, column=2, padx=5)
        self.date_filter_var = tk.StringVar(value="Invoice Date")
        self.date_filter_cb = ttk.Combobox(self.filter_frame, textvariable=self.date_filter_var, values=["Invoice Date", "Check Date"], width=12, state="readonly", justify="center")
        self.date_filter_cb.grid(row=0, column=3, padx=5, sticky="ew")
        self.date_filter_cb.bind("<<ComboboxSelected>>", self.company_entry.on_select)

        # Date ranges
        ttk.Label(self.filter_frame, text="Start date:").grid(row=0, column=4, padx=5)
        self.start_entry = DateEntry(self.filter_frame, width=10, date_pattern="mm/dd/yyyy")
        self.start_entry.set_date("01/01/2014")
        self.start_entry.grid(row=0, column=5, padx=5)
        self.start_entry.bind("<<DateEntrySelected>>", self.company_entry.on_select)
        self.start_entry.bind("<Return>", self.company_entry.on_select)

        ttk.Label(self.filter_frame, text="End date:").grid(row=0, column=6, padx=5)
        self.end_entry = DateEntry(self.filter_frame, width=10, date_pattern="mm/dd/yyyy")
        self.end_entry.grid(row=0, column=7, padx=5)
        self.end_entry.bind("<Return>", self.company_entry.on_select)
        self.end_entry.bind("<<DateEntrySelected>>", self.company_entry.on_select)

        # All companies checkbox
        self.all_companies = tk.BooleanVar()
        self.all_companies_cb = ttk.Checkbutton(self.filter_frame, text="View All Companies", variable=self.all_companies, command=self.company_entry.toggle_all_companies, takefocus=False)
        self.all_companies_cb.grid(row=0, column=8, padx=5)

        # Search names checkbox - when on, the company box also matches vendor names, not just IDs
        self.search_names = tk.BooleanVar()
        self.search_names_cb = ttk.Checkbutton(self.filter_frame, text="Search Names", variable=self.search_names, command=self.company_entry.on_select, takefocus=False)
        self.search_names_cb.grid(row=1, column=8, padx=5, sticky="w")

        # PDF Only Checkbox
        self.pdf_only = tk.BooleanVar()
        self.pdf_cb = ttk.Checkbutton(self.filter_frame, text="File Available Only", variable=self.pdf_only, command=self.company_entry.on_select, takefocus=False)
        self.pdf_cb.grid(row=0, column=9, padx=5)

        # Far right frame for buttons
        self.filter_frame.columnconfigure(10, weight=1)

        self.right_button_frame = ttk.Frame(self.filter_frame)
        self.right_button_frame.grid(row=0, column=12, padx=(0, 15), sticky="e")
        # Refresh button
        self.refresh_button = tk.Button(self.right_button_frame, text="⭮", command=self.restart)
        self.refresh_button.pack(side="left", padx=2)

        # Help button
        self.help_button = tk.Button(self.right_button_frame, text="Help", command=lambda *_: self.help_popup.toggle())
        self.help_button.pack(side="left", padx=2)

        # Errors button
        self.errors_button = tk.Button(self.right_button_frame, text="Errors", command=lambda *_: self.error_popup.toggle())
        self.errors_button.pack(side="left", padx=2)

        # Row 1

        # Invoice search
        ttk.Label(self.filter_frame, text="Invoice:").grid(row=1, column=0, padx=5)
        self.invoice_entry = tk.Entry(self.filter_frame)
        self.invoice_entry.grid(row=1, column=1, padx=5)
        self.invoice_text = tk.StringVar()
        self.prev_invoice_text = ""
        self.invoice_entry["textvariable"] = self.invoice_text
        self.invoice_text.trace_add("write", lambda *_: self.company_entry.debounced_select(source="invoice"))

        # Account Search bar
        ttk.Label(self.filter_frame, text="Account:").grid(row=1, column=2, padx=5)
        self.account_entry = tk.Entry(self.filter_frame)
        self.account_entry.grid(row=1, column=3, padx=5)
        self.account_text = tk.StringVar()
        self.account_entry["textvariable"] = self.account_text
        self.prev_account_text = ""
        # When account text changes, rebuild using the new account filter
        self.account_text.trace_add("write", lambda *_: self.company_entry.debounced_select(source="account"))

        # Plant Filter Dropdown
        ttk.Label(self.filter_frame, text="Plant:").grid(row=1, column=4, padx=5)
        self.plant_var = tk.StringVar(value="Both")
        self.plant_cb = ttk.Combobox(self.filter_frame, textvariable=self.plant_var, values=["Both", "ACP", "APC"], width=6, state="readonly", justify="center")
        self.plant_cb.grid(row=1, column=5, padx=5, sticky="ew")
        self.plant_cb.bind("<<ComboboxSelected>>", self.company_entry.on_select)

        # Clear Filters Button
        self.clear_button = tk.Button(self.filter_frame, text="Clear Filters", command=self.clear_filters)
        self.clear_button.grid(row=1, column=6, columnspan=2, padx=5, sticky="ew")


    def create_summary_bar(self):
        # Bottom level summary bar for totals and selected sum
        self.summary_frame = ttk.Frame(self, relief="groove", borderwidth=1, padding=(6, 4))
        self.summary_frame.grid(row=2, column=0, sticky="ew")
        
        for i in range(5):
            self.summary_frame.columnconfigure(i, weight=1)

        self.amount_label = ttk.Label(self.summary_frame, text="0 invoices found.", font=("TKDefaultFont", 10, "bold"))
        self.amount_label.grid(row=0, column=0, sticky="w", padx=10)

        self.account_sum = tk.StringVar(value="Account Total: $0.00")
        self.account_sum_label = ttk.Label(self.summary_frame, textvariable=self.account_sum, font=("TKDefaultFont", 10))
        self.account_sum_label.grid(row=0, column=1, sticky="w")

        self.selected_sum = tk.StringVar(value="Selected Total: $0.00")
        self.selected_sum_label = ttk.Label(self.summary_frame, textvariable=self.selected_sum, font=("TKDefaultFont", 10))
        self.selected_sum_label.grid(row=0, column=2, sticky="w")

        self.invoice_total = tk.StringVar(value="Invoice Total: $0.00")
        self.invoice_total_label = ttk.Label(self.summary_frame, textvariable=self.invoice_total, font=("TKDefaultFont", 10, "bold"))
        self.invoice_total_label.grid(row=0, column=3, sticky="w")

        self.balance_total = tk.StringVar(value="Balance Total: $0.00")
        self.balance_total_label = ttk.Label(self.summary_frame, textvariable=self.balance_total, font=("TKDefaultFont", 10, "bold"))
        self.balance_total_label.grid(row=0, column=4, sticky="w", padx=10)


    def create_treeview(self):
        self.tree_frame = ttk.Frame(self, height=600)
        self.tree_frame.grid(row=1, column=0, sticky="nsew")

        self.tree = ttk.Treeview(self.tree_frame, columns=("Vendor", "Company Name", "GL Account", "Invoice", "Date", "Invoice Amount", "Balance", 
                                                           "Check Number", "Check Date", "File Available", "Filepath"), show='tree headings')
        self.tree.column("#0", width=0, stretch=False)
        self.tree.column("Vendor", width=50, anchor="center")
        self.tree.column("Company Name", width=110, anchor="center")
        self.tree.column("GL Account", width=140, anchor="center")
        self.tree.column("Invoice", width=110, anchor="center")
        self.tree.column("Date", width=40, anchor="center")
        self.tree.column("Invoice Amount", width=50, anchor="center")
        self.tree.column("Balance", width=50, anchor="center")
        self.tree.column("Check Number", width=70, anchor="center")
        self.tree.column("Check Date", width=40, anchor="center")
        self.tree.column("File Available", width=30, anchor="center")
        self.tree.column("Filepath", width=0, stretch=False)

        self.tree.heading("Vendor", text="Vendor", command=lambda: self.sort_by("Vendor"))
        self.tree.heading("Company Name", text="Company Name", command=lambda: self.sort_by("Company Name"))
        self.tree.heading("GL Account", text="GL Account", command=lambda: self.sort_by("GL Account"))
        self.tree.heading("Invoice", text="Invoice", command=lambda: self.sort_by("Invoice"))
        self.tree.heading("Date", text="Date  ▼", command=lambda: self.sort_by("Date"))
        self.tree.heading("Invoice Amount", text="Invoice Amount", command=lambda: self.sort_by("Invoice Amount"))
        self.tree.heading("Balance", text="Balance", command=lambda: self.sort_by("Balance"))
        self.tree.heading("Check Number", text="Check Number", command=lambda: self.sort_by("Check Number"))
        self.tree.heading("Check Date", text="Check Date", command=lambda: self.sort_by("Check Date"))
        self.tree.heading("File Available", text="File Available", command=lambda: self.sort_by("File Available"))
        self.tree.heading("Filepath", text="")
        self.tree.pack(side="left", fill="both", expand=True)

        self.tree_scrollbar = ttk.Scrollbar(self.tree_frame, command=self.tree.yview)
        self.tree_scrollbar.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=self._on_tree_scroll)
        self.tree.bind("<<TreeviewSelect>>", self.update_selected_sum)

        self.style.configure("Treeview", rowheight=20) 
        self.tree.tag_configure("oddrow",  background="#f7f7f7")
        self.tree.tag_configure("evenrow", background="#ffffff")
        self.tree.tag_configure("checkrow", background="#fdfaf1")


    def _on_tree_scroll(self, first, last):
        self.tree_scrollbar.set(first, last)
        if self._loading_page or self.displayed_count >= len(self.current_rows):
            return
        try:
            near_bottom = float(last) >= 0.92
        except (TypeError, ValueError):
            return
        if near_bottom:
            self.after_idle(self.load_more_rows)


    def _insert_result_row(self, row, index):
        display = list(row)
        vendor, invoice = row[0], row[3]

        gl_accounts = self.accounts_by_vendor_invoice[(vendor, invoice)]
        if len(gl_accounts) > 1:
            matching = [(acct, amt) for acct, amt in gl_accounts
                        if acct is not None and self.account_match_filter(self.current_account_filter, acct)]
            if matching:
                display[2] = "▼"

        checks = self.checks_by_vendor_invoice[(vendor, invoice)]
        if len(checks) > 1:
            if self.date_filter_var.get() == "Check Date":
                start_date = self.start_entry.get_date()
                end_date = self.end_entry.get_date()
                checks = [c for c in checks if start_date <= c[1].date() <= end_date]
            if checks:
                display[7] = "▼"

        tag = "evenrow" if index % 2 == 0 else "oddrow"
        self.tree.insert("", "end", values=display, tags=tag)


    def load_more_rows(self, reset=False):
        if self._loading_page:
            return
        self._loading_page = True
        try:
            if reset:
                self.tree.delete(*self.tree.get_children())
                self.displayed_count = 0

            start = self.displayed_count
            end = min(start + self.page_size, len(self.current_rows))
            for i in range(start, end):
                self._insert_result_row(self.current_rows[i], i)
            self.displayed_count = end
        finally:
            self._loading_page = False
        self.update_result_label()


    def update_result_label(self):
        total = self.result_count
        if total == 0:
            self.amount_label.config(text="No invoices found.")
        elif total == 1:
            self.amount_label.config(text="1 invoice found.")
        elif self.displayed_count < total:
            self.amount_label.config(text=f"{total:,} invoices found. Showing {self.displayed_count:,}.")
        else:
            self.amount_label.config(text=f"{total:,} invoices found.")

    def update_selected_sum(self, *_):
        total = 0
        for item in self.tree.selection():
            vals = self.tree.item(item, "values")
            amt_str = vals[5]  # Invoice Amount column — empty on subrows
            if amt_str:
                total += float(amt_str.replace("$", "").replace("(", "-").replace(",", "").replace(")", ""))
        total_str = f"${total:,.2f}" if total >= 0 else f"(${abs(total):,.2f})"
        self.selected_sum.set(f"Selected: {total_str}")


    def show_invoices(self, company, invoice_prefix, account_filter): 
        invoice_count = 0
        invoice_total = 0
        balance_total = 0
        values = []

        plant_filter = self.plant_var.get()
        date_filter = self.date_filter_var.get()
        search = company.lower()
        invoice_search = invoice_prefix.lower()
        search_names = self.search_names.get()
        all_companies = self.all_companies.get()
        pdf_only = self.pdf_only.get()
        ignoring = self.ignoring
        ignore_list = self.ignore_list
        start_date = self.start_entry.get_date()
        end_date = self.end_entry.get_date()

        for entry in self.invoices:
            plant_id = entry["PlantID"]
            if plant_filter == "ACP" and plant_id != "110":
                continue
            if plant_filter == "APC" and plant_id != "410":
                continue

            vendor = str(entry["VendorID"])
            if ignoring and vendor in ignore_list:
                continue
            company_name = str(entry["CompanyName"])
            vendor_l = vendor.lower()
            name_match = search_names and bool(search) and search in company_name.lower()
            if all_companies:
                if search and not (vendor_l.startswith(search) or name_match):
                    continue
            elif not (vendor_l == search or name_match):
                continue

            invoice = str(entry["InvoiceNum"])
            if account_filter:
                accounts = self.accounts_by_vendor_invoice[(vendor, invoice)]
                if not any(account and self.account_match_filter(account_filter, account) for account, _ in accounts):
                    continue

            date = entry["InvoiceDate"].date()
            checks = self.checks_by_vendor_invoice[(vendor, invoice)]
            if date_filter == "Invoice Date":
                if date < start_date or date > end_date:
                    continue
            else:
                if not any(start_date <= check_date.date() <= end_date for _, check_date, _ in checks):
                    continue

            filepath = entry.get("Filepath", "")
            has_filepath = "✔" if filepath else ""
            if pdf_only and not filepath:
                continue
            if not invoice.lower().startswith(invoice_search):
                continue

            amount_value = entry['Subtotal']
            check_number = ""
            check_date = ""
            if len(checks) == 1:
                check_number, check_date, _ = checks[0]
                check_date = check_date.strftime("%m-%d-%Y")

            payments = entry["Payments"]
            if payments == amount_value:
                balance = "Paid In Full"
            else:
                balance_value = amount_value - payments
                balance_total += balance_value
                balance = f"${balance_value:,.2f}" if balance_value >= 0 else f"(${abs(balance_value):,.2f})"
            invoice_total += amount_value
            amount = f"${amount_value:,.2f}" if amount_value >= 0 else f"(${abs(amount_value):,.2f})"

            gl_accounts = self.accounts_by_vendor_invoice[(vendor, invoice)]
            if len(gl_accounts) == 1 and gl_accounts[0][0] is not None:
                acct = gl_accounts[0][0]
                gl_account = f"{acct} - {self.account_description_by_account.get(acct, '')}"
            else:
                gl_account = ""

            values.append((vendor, company_name, gl_account, invoice, date, amount, balance,
                           check_number, check_date, has_filepath, filepath))
            invoice_count += 1

        invoice_total_s = f"${invoice_total:,.2f}" if invoice_total >= 0 else f"(${abs(invoice_total):,.2f})"
        balance_total_s = f"${balance_total:,.2f}" if balance_total >= 0 else f"(${abs(balance_total):,.2f})"
        self.invoice_total.set(f"Invoice Total: {invoice_total_s}")
        self.balance_total.set(f"Balance Total: {balance_total_s}")
        return invoice_count, values

    def filter_rows(self, company, invoice_prefix, account_filter):
        company_l = company.lower()
        invoice_l = invoice_prefix.lower()
        search_names = self.search_names.get()

        def keep(row):
            company_ok = row[0].lower().startswith(company_l) or (search_names and company_l in row[1].lower())
            if not company_ok or not row[3].lower().startswith(invoice_l):
                return False
            if account_filter:
                accounts = self.accounts_by_vendor_invoice[(row[0], row[3])]
                return any(acct and self.account_match_filter(account_filter, acct) for acct, _ in accounts)
            return True

        self.current_rows = [row for row in self.current_rows if keep(row)]
        self.current_account_filter = account_filter
        self.result_count = len(self.current_rows)

        money = lambda s: float(s.replace("$", "").replace("(", "-").replace(",", "").replace(")", "")) if s and s != "Paid In Full" else 0.0
        invoice_total = sum(money(row[5]) for row in self.current_rows)
        balance_total = sum(money(row[6]) for row in self.current_rows)
        invoice_total_s = f"${invoice_total:,.2f}" if invoice_total >= 0 else f"(${abs(invoice_total):,.2f})"
        balance_total_s = f"${balance_total:,.2f}" if balance_total >= 0 else f"(${abs(balance_total):,.2f})"
        self.invoice_total.set(f"Invoice Total: {invoice_total_s}")
        self.balance_total.set(f"Balance Total: {balance_total_s}")
        self.update_account_sum()
        self.load_more_rows(reset=True)
        return self.result_count

    def clear_filters(self):
        self.company_entry.delete(0, "end")
        self.invoice_entry.delete(0, "end")
        self.account_entry.delete(0, "end")
        self.plant_var.set("Both")
        self.date_filter_var.set("Invoice Date")
        self.start_entry.set_date("01/01/2014")
        self.end_entry.set_date(datetime.today())
        self.pdf_only.set(False)
        self.show_invoices("", "", "")
        self.update_account_sum()


    def update_account_sum(self):
        total = 0
        account_filter = self.current_account_filter
        for row in self.current_rows:
            accounts = self.accounts_by_vendor_invoice[(row[0], row[3])]
            valid = [(acct, amt) for acct, amt in accounts
                     if acct is not None and (not account_filter or self.account_match_filter(account_filter, acct))]
            if valid:
                total += sum((amt or 0) for _, amt in valid)
            elif not accounts:
                amt = row[5]
                total += float(amt.replace("$", "").replace("(", "-").replace(",", "").replace(")", ""))
        total_s = f"${total:,.2f}" if total >= 0 else f"(${abs(total):,.2f})"
        self.account_sum.set(f"Account Total: {total_s}")

    def sort_by(self, col, values=None, header_pressed=True, watch_cursor=True):
        if header_pressed:
            if col == self.sort_col:
                self.sort_desc = not self.sort_desc
            else:
                self.sort_col = col
                self.sort_desc = True

        if values is not None:
            self.current_rows = list(values)
            self.result_count = len(self.current_rows)
            self.current_account_filter = self.account_text.get()

        def invoice_key(inv):
            if inv.isdigit():
                return [(0, int(inv))]
            return [(1,)] + [(0, int(p)) if p.isdigit() else (1, p.lower()) for p in invoice_re.split(inv) if p]

        money = lambda s: float(s.replace("$", "").replace("(", "-").replace(",", "").replace(")", "")) if s and s != "Paid In Full" else 0
        keymap = {
            "Vendor": lambda x: x[0],
            "Company Name": lambda x: x[1],
            "GL Account": lambda x: x[2],
            "Invoice": lambda x: invoice_key(str(x[3])),
            "Date": lambda x: x[4],
            "Invoice Amount": lambda x: money(x[5]),
            "Balance": lambda x: money(x[6]),
            "Check Number": lambda x: x[7],
            "Check Date": lambda x: datetime.strptime(x[8], "%m-%d-%Y") if x[8] else datetime(2000, 1, 1),
            "File Available": lambda x: x[9]
        }
        reverse = self.sort_desc
        if col in ("Vendor", "Invoice", "Company Name"):
            reverse = not reverse

        if watch_cursor:
            self.config(cursor="watch")
            self.tree.config(cursor="watch")
            self.after(25, lambda: self.sort(col, keymap, reverse))
        else:
            self.sort(col, keymap, reverse)


    def sort(self, col, keymap, reverse):
        self.current_rows.sort(key=keymap[col], reverse=reverse)
        self.load_more_rows(reset=True)
        arrow = "  ▼" if self.sort_desc else "  ▲"
        for c in self.tree["columns"]:
            self.tree.heading(c, text=c + arrow if c == col else c)
        self.update_account_sum()
        self.config(cursor="")
        self.tree.config(cursor="")
        return "break"

    def account_match_filter(self, account_filter, account):
        filter = account_filter.lower()
        account = "" if account is None else str(account).strip().lower()
        description = self.account_description_by_account.get(account, "").lower()

        if filter.isdigit():
            return account.startswith(filter)
        else:
            return filter in account or filter in description

    
    def restart(self):
        # Save current filters
        try:
            target_entry = getattr(self, "company_entry", None)
            target_text = target_entry.get() if target_entry else ""
            
            self.saved_filters = {
                "search_target": target_text,
                "invoice": self.invoice_text.get(),
                "account": self.account_text.get(),
                "date_filter": self.date_filter_var.get(),
                "start_date": self.start_entry.get_date(),
                "end_date": self.end_entry.get_date(),
                "plant": self.plant_var.get(),
                "all_companies": self.all_companies.get(),
                "search_names": self.search_names.get(),
                "pdf_only": self.pdf_only.get(),
                "sort_col": self.sort_col,
                "sort_desc": self.sort_desc
            }
        except Exception:
            self.saved_filters = None
    
        # Save ignore json
        with open("ignore.json", "w") as f:
            json.dump(list(self.ignore_list), f)
        
        for w in self.winfo_children():
            w.destroy()
        
        self.after_cancel(self.loading_loop_id)
        self.invoices.clear()
        self.company_ids.clear()
        self.sort_col = "Date"
        self.sort_desc = True
        self.broken_companies.clear()
        self.broken_invoices.clear()
        self.by_vendor_invoice.clear()
        self.checks_by_vendor_invoice.clear()
        self.accounts_by_vendor_invoice.clear()
        self.missing_invoices.clear()
        self.account_description_by_account.clear()
        self.ap_by_record_num = {}
        self.cd_by_check_id = {}
        self.check_record_ids_by_ap_record.clear()
        self.check_ids_by_ap_record.clear()
        self.duplicate_invoices.clear()

        self.columnconfigure(0, weight=0)
        self.rowconfigure(0, weight=0)

        self.current_rows.clear()
        self.result_count = 0
        self.displayed_count = 0
        self._loading_page = False
        self.current_account_filter = ""

        gc.collect()

        self.create_loading_screen()
        self.loading_loop_id = self.after(50, self.loading_loop)
        threading.Thread(target=self.load_data, daemon=True).start()


    def toggle_ignore_list(self, event):
        self.ignoring = not self.ignoring
        if self.ignoring:
            self.ignore_label.grid_forget()
        else:
            self.ignore_label.grid(row=0, column=12, sticky="w", padx=39)
        self.company_entry.on_select()
        return "break"


    def add_ignore(self, event):
        vendors = ", ".join([("\n" * ((i) % 7 == 0)) + s for i, s in enumerate(self.ignore_list)])
        new_item = simpledialog.askstring("Hidden Vendors", "Current Hidden Vendors:\n" + vendors + "\n\nEnter new Vendor ID (case doesn't matter)", parent=self)
        if new_item:
            self.ignore_list.add(new_item.upper())
        return "break"
    

    def on_exit(self):
        with open("ignore.json", "w") as f:
            json.dump(list(self.ignore_list), f)
        self.destroy()


class AutoCompleteEntry(tk.Entry):
    def __init__(self, root: InvoiceViewer, *a, **kw):
        super().__init__(root.filter_frame, *a, **kw)
        self.invoices = root.invoices
        self.company_ids = root.company_ids
        self.tree = root.tree
        self.root = root
        self.listbox = None
        self.company = tk.StringVar()
        self.prev_company = ""
        self["textvariable"] = self.company 
        self.search_job = None 

        self.text_trace = self.company.trace_add("write", self.show_suggestions)
        self.bind("<Return>", self.on_select)
        self.bind("<Up>", lambda *_: self.listbox_move("up"))
        self.bind("<Down>", lambda *_: self.listbox_move("down"))
        self.bind("<Escape>", self.close_listbox)
        self.tree.bind("<ButtonPress-1>", self.on_row_click, True)
        self.tree.bind("<Button-3>", self.show_cell_menu, True)
        #self.tree.bind("<Double-1>", self.open_file)


    def show_suggestions(self, *_):
        # Get current text and find matches
        text = self.company.get()
        if not text:
            self.close_listbox()
            return

        matches = [w for w in self.company_ids
                   if (w[0].lower().startswith(text.lower())
                       or (self.root.search_names.get() and text.lower() in w[1].lower()))
                   and (not self.root.ignoring or not w[2])]
        if not matches:
            self.close_listbox()
            return

        if self.listbox is None:
            self.listbox = ttk.Treeview(self.root, columns=("id", "name"), show="tree", height=8)
            self.listbox.heading("id", text="ID")
            self.listbox.heading("name", text="Name")
            self.listbox.bind("<ButtonRelease-1>", self.on_select)
            self.listbox.bind("<Return>", self.on_select)
            self.listbox.bind("<Up>", lambda e: self.listbox_move("up"))
            self.listbox.bind("<Down>", lambda e: self.listbox_move("down"))

        self.listbox.delete(*self.listbox.get_children())
        matches.sort()  # Sort matches alphabetically
        for w in matches:
            self.listbox.insert("", tk.END, values=(w[0], w[1]))

        # position the listbox just under the entry widget
        x = self.winfo_x()
        y = self.winfo_y() + self.winfo_height() + 7
        self.listbox.place(x=x, y=y)


    def on_select(self, *_, source=None):
        # Get selected company and update treeview
        if not self.root.all_companies.get():
            if self.listbox: 
                selection = self.listbox.selection()
                if selection:
                    self.company.set(self.listbox.item(selection[0], "values")[0])
                else:
                    items = self.listbox.get_children()
                    if len(items) == 1:
                        self.company.set(self.listbox.item(items[0], "values")[0])

            company = self.company.get()
            if self.root.search_names.get():
                company_l = company.lower()
                valid = any(tup[0].lower() == company_l or company_l in tup[1].lower() for tup in self.company_ids)
            else:
                valid = any(company in tup for tup in self.company_ids)
            if not valid:
                self.tree.delete(*self.tree.get_children())
                return
        else:
            company = self.company.get()
            
        if self.root.ignoring and company in self.root.ignore_list:
            self.tree.delete(*self.tree.get_children())
            return
        invoice_prefix = self.root.invoice_text.get()
        account_filter = self.root.account_text.get()

        # If user adds text, just need to filter not re add all rows
        narrow = False
        if ((source == "company" and company.startswith(self.prev_company) and not company == self.prev_company) or
            (source == "invoice" and invoice_prefix.startswith(self.root.prev_invoice_text) and not invoice_prefix == self.root.prev_invoice_text) or
            (source == "account" and account_filter.startswith(self.root.prev_account_text) and not account_filter == self.root.prev_account_text)):
                narrow = True
        self.prev_company = company
        self.root.prev_invoice_text = invoice_prefix
        self.root.prev_account_text = account_filter

        # Set loading mouse icon
        self.root.config(cursor="watch")
        self.config(cursor="watch")
        self.root.tree.config(cursor="watch")
        self.root.amount_label.config(text="Loading...")
        self.after(25, lambda: self.search(company, invoice_prefix, account_filter, narrow))

    
    def search(self, company, invoice_prefix, account_filter, narrow):
        if narrow and self.root.current_rows:
            invoice_count = self.root.filter_rows(company, invoice_prefix, account_filter)
        else:
            invoice_count, values = self.root.show_invoices(company, invoice_prefix, account_filter)
            self.root.current_account_filter = account_filter
            self.root.sort_by(self.root.sort_col, values, header_pressed=False, watch_cursor=False)

        self.root.result_count = invoice_count
        self.root.update_result_label()
        self.root.config(cursor="")
        self.config(cursor="")
        self.root.tree.config(cursor="")
        self.close_listbox()

    def listbox_move(self, dir):
        if not self.listbox:
            return
        
        dir = 1 if dir == "down" else -1
        
        rows = self.listbox.get_children()
        if not rows:
            return

        current = self.listbox.selection()
        i = rows.index(current[0]) if current else -1
        i = (i + dir) % len(rows)
        self.listbox.selection_set(rows[i])
        self.listbox.focus(rows[i])
        self.listbox.see(rows[i])


    def on_row_click(self, event):
        if self.tree.identify_region(event.x, event.y) != "cell":
            return
    
        row = self.tree.identify_row(event.y)
        if not row:
            return
        
        col_num = self.tree.identify_column(event.x)
        col = self.tree.heading(col_num)["text"]

        if col == "GL Account" or col == "GL Account  ▲" or col == "GL Account  ▼":
            self.toggle_gl_accounts(row)
            return "break"
        elif col == "Check Number" or col == "Check Number  ▲" or col == "Check Number  ▼":
            self.toggle_checks(row)
            return "break"
        else:
            self.open_file(event)


    def show_cell_menu(self, event):
        # Only respond to right-clicks on an actual data cell
        if self.tree.identify_region(event.x, event.y) != "cell":
            return

        row = self.tree.identify_row(event.y)
        if not row:
            return

        col_id = self.tree.identify_column(event.x)  # e.g. "#4"; "#0" is the hidden tree column
        if col_id == "#0":
            return

        col_index = int(col_id[1:]) - 1
        columns = self.tree["columns"]
        if col_index < 0 or col_index >= len(columns):
            return
        col_name = columns[col_index]

        # If the right-clicked row is part of an existing multi-row selection, keep that
        # selection; otherwise select just the row under the cursor.
        selection = self.tree.selection()
        if row in selection and len(selection) > 1:
            rows = self.ordered_selection()
        else:
            self.tree.selection_set(row)
            self.tree.focus(row)
            rows = [row]

        # Strip any sort arrows from the heading for a clean menu label
        heading = self.tree.heading(col_name)["text"].replace("  ▼", "").replace("  ▲", "").strip()

        menu = tk.Menu(self.tree, tearoff=0)
        if len(rows) > 1:
            n = len(rows)
            menu.add_command(label=f"Copy {heading} ({n} rows)", command=lambda: self.copy_column(rows, col_name))
            menu.add_command(label=f"Copy Rows ({n} rows)", command=lambda: self.copy_rows(rows))
        else:
            # Show Invoice Record Number
            parent_id = self.tree.parent(row)
            target_row = parent_id if parent_id else row
            
            vendor = self.tree.set(target_row, "Vendor")
            invoice = self.tree.set(target_row, "Invoice")
            
            if vendor and invoice:
                # Fetch full row data from the dictionary built during load
                row_data = self.root.by_vendor_invoice.get((vendor, invoice))
                if row_data:
                    company_name = str(row_data.get("CompanyName", "")).strip()

                    if "RecordNum" in row_data:
                        record_num = row_data["RecordNum"]
                        
                        # 1. Record Number
                        menu.add_command(label=f"Record Number: {record_num}", state="disabled")

                        # 2. Check Detail Record IDs
                        check_rec_ids = self.root.check_record_ids_by_ap_record.get(record_num, [])
                        for check_rec_id in check_rec_ids:
                            menu.add_command(label=f"Check Detail ID: {check_rec_id}", state="disabled")

                        # 3. Check IDs (Mapped via CheckID)
                        check_ids = self.root.check_ids_by_ap_record.get(record_num, [])
                        for check_id in check_ids:
                            menu.add_command(label=f"Check ID: {check_id}", state="disabled")

                        # 4. AP Number (Mapped via RecordNum)
                        ap_num = self.root.ap_by_record_num.get(record_num)
                        if ap_num:
                            menu.add_command(label=f"Journal ID: {ap_num}", state="disabled")

                        # 5. CD Number (Mapped via CheckID)
                        for check_id in check_ids:
                            cd_num = self.root.cd_by_check_id.get(check_id)
                            if cd_num:
                                menu.add_command(label=f"Journal ID: {cd_num}", state="disabled")
                            
                        # Add separator after informational headers
                        menu.add_separator()

            value = self.tree.set(row, col_name)
            # Disable the cell copy if there's nothing meaningful to copy (blank / arrows / checkmark)
            if value and value not in ("▼", "▲", "✔"):
                menu.add_command(label=f"Copy {heading}", command=lambda: self.copy_to_clipboard(value))
            else:
                menu.add_command(label=f"Copy {heading}", state="disabled")
            menu.add_command(label="Copy Row", command=lambda: self.copy_row(row))
            menu.add_command(label="Copy Date & Invoice", command=lambda: self.copy_date_invoice(row))

        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()


    def ordered_selection(self):
        # Selected rows in top-to-bottom display order (parents before their subrows)
        selected = set(self.tree.selection())
        ordered = []
        def walk(parent):
            for child in self.tree.get_children(parent):
                if child in selected:
                    ordered.append(child)
                walk(child)
        walk("")
        return ordered


    def copy_to_clipboard(self, text):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)


    def row_text(self, row):
        # Visible columns for one row, tab-separated (skips the hidden Filepath helper column)
        cells = []
        for col in self.tree["columns"]:
            if col == "Filepath":
                continue
            val = self.tree.set(row, col)
            if val in ("▼", "▲"):  # collapse/expand arrows aren't real data
                val = ""
            cells.append(val)
        return "\t".join(cells)


    def copy_row(self, row):
        self.copy_to_clipboard(self.row_text(row))


    def copy_date_invoice(self, row):
        # Copy just the date and invoice number for one row, tab-separated
        date = self.tree.set(row, "Date")
        invoice = self.tree.set(row, "Invoice")
        date_formatted = datetime.strptime(date, "%Y-%m-%d").strftime("%m-%d-%y")
        self.copy_to_clipboard(f"{date_formatted}_{invoice}")


    def copy_column(self, rows, col_name):
        # One line per selected row, using that row's value for the given column
        lines = []
        for row in rows:
            val = self.tree.set(row, col_name)
            if val in ("▼", "▲"):
                val = ""
            lines.append(val)
        self.copy_to_clipboard("\n".join(lines))


    def copy_rows(self, rows):
        # One selected row per line, each tab-separated across its visible columns
        self.copy_to_clipboard("\n".join(self.row_text(row) for row in rows))


    def _ensure_invoice_children(self, row):
        if self.tree.get_children(row):
            return

        vendor = self.tree.set(row, "Vendor")
        invoice = self.tree.set(row, "Invoice")

        gl_accounts = self.root.accounts_by_vendor_invoice[(vendor, invoice)]
        if len(gl_accounts) > 1:
            matching = [(acct, amt) for acct, amt in gl_accounts
                        if acct is not None and self.root.account_match_filter(self.root.current_account_filter, acct)]
            matching.sort(key=lambda x: x[0])
            if matching:
                self.tree.set(row, "GL Account", "▼")
                for acct, amt in matching:
                    amt = amt or 0
                    amt_s = f"${amt:,.2f}" if amt >= 0 else f"(${abs(amt):,.2f})"
                    acct_s = f"{acct} - {self.root.account_description_by_account.get(acct, '')}"
                    self.tree.insert(row, "end", values=("", "", acct_s, amt_s, "", "", "", "", "", "", ""), tags="checkrow")

        checks = list(self.root.checks_by_vendor_invoice[(vendor, invoice)])
        if len(checks) > 1:
            if self.root.date_filter_var.get() == "Check Date":
                start_date = self.root.start_entry.get_date()
                end_date = self.root.end_entry.get_date()
                checks = [c for c in checks if start_date <= c[1].date() <= end_date]
            checks.sort(key=lambda x: x[1], reverse=True)
            if checks:
                self.tree.set(row, "Check Number", "▼")
                for cnum, cdate, camt in checks:
                    cdate_s = cdate.strftime("%m-%d-%Y")
                    camt = camt or 0
                    camt_s = f"${camt:,.2f}" if camt >= 0 else f"(${abs(camt):,.2f})"
                    self.tree.insert(row, "end", values=("", "", "", "", "", "", camt_s, cnum, cdate_s, "", ""), tags="checkrow")


    def toggle_checks(self, row):
        if self.tree.set(row, "Check Number") not in ("▼", "▲"):
            return
        self._ensure_invoice_children(row)
        is_open = self.tree.item(row, "open")
        new_open = not is_open
        self.tree.set(row, "Check Number", "▲" if new_open else "▼")
        if self.tree.set(row, "GL Account") in ("▲", "▼"):
            self.tree.set(row, "GL Account", "▲" if new_open else "▼")
        self.tree.item(row, open=new_open)

    def toggle_gl_accounts(self, row):
        if self.tree.set(row, "GL Account") not in ("▼", "▲"):
            return
        self._ensure_invoice_children(row)
        is_open = self.tree.item(row, "open")
        new_open = not is_open
        self.tree.set(row, "GL Account", "▲" if new_open else "▼")
        if self.tree.set(row, "Check Number") in ("▲", "▼"):
            self.tree.set(row, "Check Number", "▲" if new_open else "▼")
        self.tree.item(row, open=new_open)

    def toggle_all_companies(self):
        if self.root.all_companies.get():
            self.company.trace_remove("write", self.text_trace)
            self.text_trace = self.company.trace_add("write", lambda *_: self.debounced_select(source="company"))
        else:
            self.company.trace_remove("write", self.text_trace)
            self.text_trace = self.company.trace_add("write", self.show_suggestions)
        self.on_select()


    def debounced_select(self, *args, source=None):
        if self.search_job is not None:
            self.after_cancel(self.search_job)
        
        self.search_job = self.after(300, lambda: self.on_select(*args, source=source))


    def close_listbox(self, *_):
        if self.listbox:
            self.listbox.destroy()
            self.listbox = None

    
    def open_file(self, event):
        try:
            selection = self.tree.selection()
            if not selection:
                return
            
            if self.tree.identify_region(event.x, event.y) != "cell":
                return
        
            row = self.tree.identify_row(event.y)
            if not row:
                return
            
            if selection[0] != row:
                return
            
            item = selection[0]
            filepath = self.tree.set(item, "Filepath")
            if not filepath:
                return
            if not os.path.exists(filepath):
                messagebox.showerror("Error", "File not found.")
                return
            
            os.startfile(filepath)
        except Exception as e:
            messagebox.showerror("Error", f"Failed to open file: {e}")


class ErrorPopup(tk.Toplevel):
    def __init__(self, root, terrors, ierrors, missing:list[tuple], duplicates:list[str], **kw):
        super().__init__(root, **kw)
        self.root = root
        self.title("Error Page")
        self.wm_attributes("-toolwindow", True)
        self.withdraw()
        self.attributes("-topmost", True)
        self.protocol("WM_DELETE_WINDOW", self.toggle)
        self.bind("<space>", self.toggle)
        self.geometry("750x400")

        frame = tk.Frame(self)
        frame.pack(expand=True, fill="both")

        self.text = scrolledtext.ScrolledText(frame, font=font.Font(family="Consolas", size=9), wrap="none")
        self.text.pack(expand=True, fill="both")
        hscroll = tk.Scrollbar(self, orient="horizontal", command=self.text.xview)
        hscroll.pack(side="bottom", fill="x")
        self.text.configure(xscrollcommand=hscroll.set)
        self.text.tag_configure("bold", font=font.Font(family="Consolas", size=12, weight="bold"))
        
        # Titan errors
        terrors.sort(key=lambda x: x["VendorID"])
        self.text.insert(tk.END, f" {len(terrors)} Titan Invoice Errors\n", ("bold",))
        for row in terrors:
            self.text.insert(tk.END, f" -{row}\n")

        # File errors
        self.text.insert(tk.END, f"\n {len(ierrors)} Invoice File Errors\n", ("bold",))
        for row in ierrors:
            self.text.insert(tk.END, f" -{row}\n")

            
        # Duplicate Invoices
        if duplicates:
            self.text.insert(tk.END, f"\n {len(duplicates)} Duplicate Invoices Found in Database\n", ("bold",))
            for row in duplicates:
                self.text.insert(tk.END, f" -{row}\n")

        # Missing invoice files
        missing.sort(key=lambda x: (x[0], -datetime.strptime(x[2], "%m-%d-%Y").timestamp()))
        self.text.insert(tk.END, f"\n {len(missing)} Invoices Missing Files\n", ("bold",))
        for row in missing:
            self.text.insert(tk.END, f" -{row}\n")

    
    def toggle(self, *_):
        if self.state() == "withdrawn":
            self.show()
        else:
            self.withdraw()


    def show(self):
        # center on top of anchor window
        ax, ay = self.root.winfo_rootx(), self.root.winfo_rooty()
        aw, ah = self.root.winfo_width(), self.root.winfo_height()
        w, h = self.winfo_width(), self.winfo_height()
        x = ax + (aw - w) // 2
        y = ay + (ah - h) // 3
        self.geometry(f"+{x}+{y}")
        self.deiconify()


class HelpPopup(tk.Toplevel):
    def __init__(self, root, **kw):
        super().__init__(root, **kw)
        self.root = root
        self.title("Help Page")
        self.wm_attributes("-toolwindow", True)
        self.withdraw()
        self.attributes("-topmost", True)
        self.protocol("WM_DELETE_WINDOW", self.toggle)
        self.bind("<space>", self.toggle)
        self.geometry("700x600")

        frame = tk.Frame(self)
        frame.pack(expand=True, fill="both")

        self.text = scrolledtext.ScrolledText(frame, font=font.Font(family="Consolas", size=9), wrap="none")
        self.text.pack(expand=True, fill="both")

        
        self.text.tag_configure("header", font=font.Font(family="Consolas", size=10, weight="bold"), spacing1=8)
        self.text.tag_configure("title",  font=font.Font(family="Consolas", size=12, weight="bold"), spacing1=4)
        self.text.tag_configure("body",   font=font.Font(family="Consolas", size=9), lmargin1=10, lmargin2=10)
        self.text.tag_configure("indent", font=font.Font(family="Consolas", size=9), lmargin1=26, lmargin2=26)
        self.text.tag_configure("footer", font=font.Font(family="Consolas", size=9), spacing1=10)

        def h(text): self.text.insert(tk.END, text + "\n", "header")
        def b(text): self.text.insert(tk.END, text + "\n", "body")
        def i(text): self.text.insert(tk.END, text + "\n", "indent")
        def f(text): self.text.insert(tk.END, text + "\n", "footer")

        self.text.insert(tk.END, "Titan AP Invoice Viewer — Help\n", "title")

        h("GETTING STARTED")
        b("When the program opens it loads all invoice data from the Titan database and scans")
        b("the invoice file directory. This takes a few seconds. Once loading is complete the")
        b("main invoice table and search bar will appear")
        b("• Press the ⭮ Refresh button (top-right) at any time to reload the latest data")
        b("• Press the Errors button to see any invoices or files that had problems loading")

        h("SEARCHING FOR INVOICES")
        b("Company ID  — Type a vendor ID into the Company ID box. A suggestion list will")
        b("appear; click a result or press Enter to load that vendor's invoices")
        b("")
        b("View All Companies  — Check this box to show invoices across all vendors at once")
        b("In this mode the Company ID box becomes a prefix filter: typing 'AC' shows every")
        b("vendor whose ID starts with 'AC', rather than requiring an exact match")
        b("")
        b("Search Names  — Check this box to also match vendor names, not just IDs. With it on,")
        b("typing part of a name (e.g. 'concrete') finds every vendor whose name contains that")
        b("text, and the suggestion list shows those matches too. Works alongside the options")
        b("above. Leave it off to search by vendor ID only")
        b("")
        b("Invoice  — Type in the Invoice box to narrow results to invoices whose number")
        b("starts with your entry")
        b("")
        b("Account  — Filter by GL account number or description. Entering digits matches")
        b("account numbers starting with those digits. Entering text matches any account")
        b("whose number or description contains that text")
        b("")
        b("Plant — Filter invoices by which plant they belong to:")
        i("Both  — Show invoices from all plants (default)")
        i("ACP   — Show only Atlantic Concrete invoices (Plant ID 110)")
        i("APC   — Show only Atlantic Precast invoices (Plant ID 410)")
        b("")
        b("Start / End Date  — Only invoices within this date range will be shown. The")
        b("Date Filter dropdown controls what kind of date is being filtered:")
        i("Invoice Date  — Filters by the date printed on the invoice (default)")
        i("Check Date    — Filters by the date the payment check was issued.")
        b("                Note: in Check Date mode, invoices with no associated checks")
        b("                are hidden, and only checks within the date range are shown")
        b("                when expanding a multi-check row")
        b("")
        b("File Available Only  — Check this box to hide any invoices that do not have a")
        b("PDF file stored on the network drive")

        h("THE INVOICE TABLE")
        b("Each row is one invoice. The columns show:")
        i("Vendor          — The vendor ID code")
        i("Company Name    — The vendor's full company name")
        i("GL Account      — The expense account this invoice was coded to")
        i("Invoice         — The invoice number")
        i("Date            — The invoice date")
        i("Invoice Amount  — The total dollar amount of the invoice")
        i("Balance         — The remaining unpaid amount, or 'Paid In Full'")
        i("Check Number    — The check used to pay this invoice")
        i("Check Date      — The date that check was issued")
        i("File Available  — A ✔ means a PDF of the invoice is stored on the network")

        h("EXPANDING ROWS — GL ACCOUNTS AND CHECKS")
        b("Some invoices are split across multiple expense accounts or were paid by more")
        b("than one check. These rows show a ▼ arrow in the GL Account or Check Number")
        b("column")
        b("• Click the ▼ in the GL Account column to expand and see each individual account")
        b("  line with its account number, description, and amount. Account lines are")
        b("  sorted by account number")
        b("• Click the ▼ in the Check Number column to expand and see each check with its")
        b("  number, date, and payment amount. Checks are sorted newest first")
        b("• Click the ▲ again to collapse the row")

        h("OPENING INVOICE FILES")
        b("Double left-click any row that has a ✔ in the File Available column to open the")
        b("invoice PDF")

        h("DATABASE REFERENCES AND COPYING DATA")
        b("Right-click any cell to open a small menu")
        i("The top of the menu shows the invoice's Record Number, Check Record ID, Check ID, AP Journal ID, and CD Journal ID(s) if available")
        i("The Record Number can be used to find invoices in the AP_Header table, and the Journal IDs can be used for the AP_Journal_Detail_Source table")
        i("The Check Detail ID can be used to find invoices in the Check_Detail table, while the Check ID can also be used for the Check_Header table")
        i("Copy <Column>   — Copies just that cell, e.g. an invoice number or GL account")
        i("Copy Row — Copies the whole row, tab-separated (pastes neatly into Excel)")
        i("Copy Date & Invoice — Copies the date and invoice number for one row, underscore-separated")
        b("")
        b("To copy several rows at once, select them first (Ctrl-click or Shift-click), then")
        b("right-click any cell within the selection. The menu changes to:")
        i("Copy <Column> (N rows)    — Copies that one column's value from every selected row,")
        i("                            one per line")
        i("Copy Rows (N rows) — Copies every selected row in full, one row per line")
        b("Both multi-row options paste straight into Excel as rows and columns")

        h("SORTING")
        b("Click any column header to sort the table by that column. Click the same header")
        b("again to reverse the sort direction. The active sort column is marked with ▲ or ▼")

        h("SELECTING ROWS AND TOTALS")
        b("Click a row to select it. The totals bar at the bottom of the window shows:")
        i("Account Total  — Sum of GL distribution amounts for all visible invoices")
        i("Selected Total — Sum of Invoice Amount for only the rows you have selected")
        i("Invoice Total  — Sum of Invoice Amount for all visible invoices")
        i("Balance Total  — Sum of outstanding balances for all visible invoices")
        b("To select multiple rows hold Ctrl and click individual rows, or hold Shift and")
        b("click to select a continuous range")

        f("Questions or suggestions can be sent to jmwesthoff@atlanticconcrete.com")


    def toggle(self, *_):
        if self.state() == "withdrawn":
            self.show()
        else:
            self.withdraw()


    def show(self):
        # center on top of anchor window
        ax, ay = self.root.winfo_rootx(), self.root.winfo_rooty()
        aw, ah = self.root.winfo_width(), self.root.winfo_height()
        w, h = self.winfo_width(), self.winfo_height()
        x = ax + (aw - w) // 2
        y = ay + (ah - h) // 3
        self.geometry(f"+{x}+{y}")
        self.deiconify()
    

if __name__ == "__main__":
    invoice_viewer = InvoiceViewer()
    invoice_viewer.mainloop()