"""Local Owner-review presentation only; importing this module creates no GUI."""
from pathlib import Path
import threading

_REVIEW_IMPORT_LOCK = threading.Lock()

BRANDING = Path(__file__).resolve().parents[2] / 'branding'
ICON_PNG = BRANDING / 'plugin-icon.png'
ICON_ICO = BRANDING / 'SoraCommander.ico'
PORTRAIT_PNG = BRANDING / 'owner-review-portrait-256.png'
COLORS = {
    'background': '#071426', 'card': '#102640', 'viewer': '#06101e',
    'text': '#edf6ff', 'secondary': '#b9d5ed', 'cyan': '#56d9ff',
    'blue': '#0967d2', 'active': '#1879e3', 'muted': '#243b55',
    'caution': '#30243f', 'amber': '#ffd1ec', 'info': '#102f4b',
}


def window(root):
    """Keep current Tk DPI scaling; leave desktop chrome space."""
    root.configure(background=COLORS['background'])
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    width = min(1536, max(1, sw - 80)) if isinstance(sw, int) else 1200
    height = min(900, max(1, sh - 100)) if isinstance(sh, int) else 720
    root.geometry(f'{width}x{height}')
    root.minsize(min(960, width), min(620, height))
    root.resizable(True, True)


def _chamfer(canvas, width, height, *, inset=2, color='#56d9ff', line=1, tag='trim'):
    """Paint only: no pointer bindings, windows, or approval commands."""
    a, b, c = inset, width - inset, height - inset
    cut = min(18, max(3, height // 5))
    canvas.create_polygon(a+cut, a, b-cut, a, b, a+cut, b, c-cut,
                          b-cut, c, a+cut, c, a, c-cut, a, a+cut,
                          fill='', outline=color, width=line, tags=tag)


def header(tk, root, subtitle):
    """Build the visual rail and return the separate review card."""
    shell = tk.Canvas(root, background=COLORS['background'], bd=0, highlightthickness=0)
    shell.pack(fill='both', expand=True, padx=8, pady=8)
    def frame(event):
        shell.delete('trim')
        for inset, color, line in ((2, '#123d60', 5), (5, '#56d9ff', 1), (10, '#194f72', 1)):
            _chamfer(shell, event.width, event.height, inset=inset, color=color, line=line)
    shell.bind('<Configure>', frame)
    shell.grid_columnconfigure(0, weight=1, uniform='review', minsize=200)
    shell.grid_columnconfigure(1, weight=3, uniform='review')
    shell.grid_rowconfigure(0, weight=1)
    rail = tk.Canvas(shell, width=220, background='#0c2043', bd=0, highlightthickness=0)
    rail.grid(row=0, column=0, sticky='nsew', padx=(16, 8), pady=16)
    # Retain originals and scaled images for the entire window lifetime.
    logo = portrait = None
    try:
        root.iconbitmap(str(ICON_ICO))
    except Exception:
        pass
    try:
        logo = tk.PhotoImage(master=root, file=str(ICON_PNG))
        root._sora_review_image = logo
        root.iconphoto(True, logo)
    except Exception:
        pass
    # This 256px portrait is the unchanged, higher-resolution PNG embedded
    # in the existing Owner-approved SoraCommander.ico (not another character).
    # A missing or invalid decoration never prevents a review or grants consent.
    try:
        portrait = tk.PhotoImage(master=root, file=str(PORTRAIT_PNG))
    except Exception:
        portrait = None
    root._sora_review_art = (logo, portrait)
    def paint(event):
        rail.delete('all')
        w, h = max(1, event.width), max(1, event.height)
        for y in range(0, h, 4):
            t = y / h
            color = '#%02x%02x%02x' % (int(18-11*t), int(53-30*t), int(91-40*t))
            rail.create_rectangle(0, y, w, y+4, fill=color, outline='')
        for x in range(12, w, 28):
            rail.create_line(x, 16, x, h-16, fill='#143756')
        for y in range(16, h, 28):
            rail.create_line(12, y, w-12, y, fill='#143756')
        _chamfer(rail, w, h, inset=3, color='#2f96c5')
        cx, cy = w/2, h*.40
        radius = min(w*.43, h*.24)
        for extra, color, thickness in ((10, '#164367', 6), (5, '#2685b5', 1), (0, '#56d9ff', 2), (-9, '#21618e', 1)):
            r = radius + extra
            rail.create_oval(cx-r, cy-r, cx+r, cy+r, outline=color, width=thickness)
        for start in (15, 110, 205, 300):
            r = radius+5
            rail.create_arc(cx-r, cy-r, cx+r, cy+r, start=start, extent=46,
                            style='arc', outline='#a1efff', width=3)
        art = portrait if portrait is not None else logo
        if art is not None:
            aw, ah = art.width(), art.height()
            if isinstance(aw, int) and isinstance(ah, int):
                # Full-sized Sora portrait dominates the normal Owner rail.
                # Shrink only when a genuinely narrow/short desktop requires it.
                max_w, max_h = max(1, w - 32), max(1, int(h * .56))
                factor = max(1, (aw + max_w - 1) // max_w,
                             (ah + max_h - 1) // max_h)
                if factor > 1:
                    art = art.subsample(factor, factor)
            root._sora_review_scaled_art = art
            rail.create_image(cx, cy, image=art)
        rail.create_text(cx, 34, text='SORA', fill='#124974', font=('Segoe UI', 32, 'bold'))
        rail.create_text(cx, 32, text='SORA', fill='#78e5ff', font=('Segoe UI', 30, 'bold'))
        rail.create_text(cx, 66, text='COMMANDER / NEXT', fill=COLORS['secondary'], font=('Segoe UI', 10, 'bold'))
        rail.create_text(cx, h*.72, text='Your AI Partner.\nMore Freedom.\nMore Possibilities.',
                         width=max(1, w-32), justify='center', fill=COLORS['text'], font=('Segoe UI', 12))
        rail.create_line(24, h-68, w-24, h-68, fill='#2b8bb9')
        rail.create_text(cx, h-40, text='◇  LOCAL ONLY', fill=COLORS['cyan'], font=('Segoe UI', 10, 'bold'))
    rail.bind('<Configure>', paint)
    main = tk.Frame(shell, background=COLORS['card'], highlightthickness=1,
                    highlightbackground='#2b6590')
    main.grid(row=0, column=1, sticky='nsew', padx=(0, 16), pady=16)
    titles = tk.Frame(main, background=COLORS['card'])
    titles.pack(fill='x', padx=14, pady=(12, 6))
    tk.Label(titles, text='❯_  OWNER REVIEW  /  LOCAL ONLY', foreground=COLORS['cyan'],
             background=COLORS['info'], font=('Segoe UI', 10, 'bold'),
             anchor='w', padx=10, pady=5).pack(fill='x')
    label = tk.Label(titles, text=subtitle, foreground=COLORS['text'],
                     background=COLORS['card'], font=('Segoe UI', 13, 'bold'),
                     anchor='w', justify='left', wraplength=760)
    label.pack(fill='x', pady=(10, 0))
    label.bind('<Configure>', lambda event: label.configure(wraplength=max(1, event.width)))
    tk.Frame(main, height=2, background=COLORS['cyan']).pack(fill='x', padx=18)
    return main


def notice(tk, root, text, *, caution=False):
    label = tk.Label(root, text=text, anchor='w', justify='left',
                     background=COLORS['caution' if caution else 'info'],
                     foreground=COLORS['amber' if caution else 'cyan'],
                     font=('Segoe UI', 10), padx=12, pady=7, wraplength=960)
    label.pack(fill='x', padx=18, pady=(6, 0))
    label.bind('<Configure>', lambda event: label.configure(wraplength=max(1, event.width - 24)))
    return label


def _scrollbar(tk, parent, command, *, vertical):
    """Dark thumb with pointer, keyboard and page navigation."""
    bar = tk.Canvas(parent, width=16, height=16, background='#102b46', bd=0,
                    highlightthickness=1, highlightbackground='#235b8c',
                    highlightcolor=COLORS['cyan'], takefocus=True)
    state = {'first': 0., 'last': 1., 'offset': 0.}
    def length():
        return max(1, bar.winfo_height() if vertical else bar.winfo_width())
    def redraw(event=None):
        bar.delete('thumb')
        a, b = state['first'] * length(), state['last'] * length()
        coords = (3, a, 13, max(a+8, b)) if vertical else (a, 3, max(a+8, b), 13)
        bar.create_rectangle(*coords, fill='#4594bf', outline='', tags='thumb')
    def set_range(first, last):
        state.update(first=float(first), last=float(last))
        redraw()
    def press(event):
        bar.focus_set()
        pos = (event.y if vertical else event.x) / length()
        state['offset'] = pos - state['first']
        if not state['first'] <= pos <= state['last']:
            command('scroll', -1 if pos < state['first'] else 1, 'pages')
            state['offset'] = 0.
    def drag(event):
        pos = (event.y if vertical else event.x) / length() - state['offset']
        command('moveto', max(0., min(1., pos)))
    bar.set = set_range
    bar.bind('<Configure>', redraw)
    bar.bind('<Button-1>', press)
    bar.bind('<B1-Motion>', drag)
    for key, amount in (('Up', -1), ('Left', -1), ('Down', 1), ('Right', 1)):
        bar.bind('<'+key+'>', lambda event, n=amount: command('scroll', n, 'units'))
    for key, amount in (('Prior', -1), ('Next', 1)):
        bar.bind('<'+key+'>', lambda event, n=amount: command('scroll', n, 'pages'))
    bar.bind('<Home>', lambda event: command('moveto', 0))
    bar.bind('<End>', lambda event: command('moveto', 1))
    return bar


def viewer(tk, root):
    """Exact read-only payload; both axes always scroll."""
    border = tk.Frame(root, background='#081a2b', highlightthickness=1,
                      highlightbackground=COLORS['cyan'], padx=4, pady=4)
    border.pack(fill='both', expand=True, padx=18, pady=10)
    root._sora_viewer_panel = border
    panel = tk.Frame(border, background=COLORS['viewer'], highlightthickness=1,
                     highlightbackground='#2e7099')
    panel.pack(fill='both', expand=True)
    pane = tk.Text(panel, height=5, wrap='none', font=('Consolas', 11),
                   background=COLORS['viewer'], foreground=COLORS['text'],
                   selectbackground=COLORS['blue'], selectforeground=COLORS['text'],
                   insertbackground=COLORS['cyan'], relief='flat', borderwidth=0,
                   padx=12, pady=10, exportselection=False, takefocus=True)
    vertical = _scrollbar(tk, panel, pane.yview, vertical=True)
    horizontal = _scrollbar(tk, panel, pane.xview, vertical=False)
    pane.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
    pane.grid(row=0, column=0, sticky='nsew')
    vertical.grid(row=0, column=1, sticky='ns')
    horizontal.grid(row=1, column=0, sticky='ew')
    panel.grid_columnconfigure(0, weight=1)
    panel.grid_rowconfigure(0, weight=1)
    return pane


def button_options(*, primary=False):
    return dict(background=COLORS['blue' if primary else 'muted'],
                foreground=COLORS['text'], activebackground=COLORS['active'],
                activeforeground=COLORS['text'], disabledforeground=COLORS['secondary'],
                font=('Segoe UI', 11, 'bold'), relief='flat', borderwidth=0,
                padx=16, pady=9, highlightthickness=2,
                highlightbackground=COLORS['background'], highlightcolor=COLORS['cyan'])


def button_bar(tk, root):
    bar = tk.Frame(root, background=COLORS['card'])
    # Allocate controls before the expanding viewer, even at minimum geometry.
    bar.pack(side='bottom', before=root._sora_viewer_panel, fill='x', padx=14, pady=(0, 12))
    for column in range(3):
        bar.grid_columnconfigure(column, weight=1, uniform='actions')
    return bar


def neon_button(tk, bar, *, column, text, command=None, primary=False, disabled=False):
    """Native Button is the only hit target; the surrounding Canvas only paints."""
    slot = tk.Canvas(bar, height=104, background=COLORS['card'], bd=0, highlightthickness=0)
    slot.grid(row=0, column=column, sticky='nsew', padx=3)
    slot.pack_propagate(False)
    options = button_options(primary=primary)
    options.update(padx=4, pady=7, highlightthickness=2, takefocus=not disabled,
                   state='disabled' if disabled else 'normal', wraplength=160)
    if disabled:
        options.update(background=COLORS['background'], disabledforeground='#91abc1')
    if command is not None and not disabled:
        options['command'] = command
    button = tk.Button(slot, text=text, **options)
    button.pack(fill='both', expand=True, padx=9, pady=10)
    state = {'hover': False, 'focus': False, 'pressed': False}
    def paint(event=None):
        w, h = slot.winfo_width(), slot.winfo_height()
        if not isinstance(w, int) or not isinstance(h, int):
            return
        slot.delete('all')
        bright = not disabled and (state['hover'] or state['focus'])
        for y in range(5, h-5, 3):
            t = y / max(1, h)
            rgb = (8, int(62-32*t), int(105-48*t)) if primary else (9, int(32-12*t), int(55-18*t))
            slot.create_line(9, y, w-9, y, fill='#%02x%02x%02x' % rgb, width=3)
        for inset, color, line in ((2, '#174c73' if primary else '#123650', 4),
                                    (4, '#b4f3ff' if bright else '#387ba0' if disabled else '#56d9ff', 2),
                                    (7, '#194c6b', 1)):
            _chamfer(slot, w, h, inset=inset, color=color, line=line)
        if state['focus']:
            slot.create_line(20, h-7, w-20, h-7, fill='#ffffff', width=2)
        button.configure(wraplength=max(1, w-30))
    def visual(key, value):
        state[key] = value
        if not disabled:
            button.configure(background=COLORS['active'] if state['pressed'] else
                             '#11558c' if state['hover'] else COLORS['blue' if primary else 'muted'])
        paint()
    slot.bind('<Configure>', paint)
    for event, key, value in (('<Enter>', 'hover', True), ('<Leave>', 'hover', False),
                               ('<FocusIn>', 'focus', True), ('<FocusOut>', 'focus', False),
                               ('<ButtonPress-1>', 'pressed', True), ('<ButtonRelease-1>', 'pressed', False),
                               ('<KeyPress-space>', 'pressed', True), ('<KeyRelease-space>', 'pressed', False)):
        button.bind(event, lambda event, k=key, v=value: visual(k, v), add='+')
    def enter(event):
        if not disabled and button.focus_get() == button and str(button.cget('state')) != 'disabled':
            button.invoke()
        return 'break'
    button.bind('<Return>', enter)
    return button


def shared_review(tk, root, *, kind, title, heading, caution, information,
                  body, reject, approve, trusted_offered=False, trusted=None):
    """Load the ONE fixed sibling renderer in both package and -I child contexts.

    This is presentation only. Adapters still own decision/expiry/nonce.
    """
    import importlib.util
    import sys
    import types
    location = Path(__file__).resolve().with_name('owner_review_renderer.py')
    spec = importlib.util.spec_from_file_location('_sora_owner_shared_renderer', location)
    if spec is None or spec.loader is None:
        raise RuntimeError('trusted Owner renderer unavailable')
    # dataclass resolves its declaring module during class creation. Keep the
    # temporary isolated-loader registration serialized across local reviews.
    with _REVIEW_IMPORT_LOCK:
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        try:
            spec.loader.exec_module(module)
            screen = module.ReviewScreen(kind, title, heading, caution, information,
                                         body, trusted_offered)
        finally:
            if sys.modules.get(spec.name) is module:
                del sys.modules[spec.name]
    present = types.SimpleNamespace(
        window=window, header=header, notice=notice, viewer=viewer,
        button_bar=button_bar, neon_button=neon_button)
    return module.render_review(tk, root, present, screen, reject=reject,
                                approve=approve, trusted=trusted)


def review_actions(tk, root, reject, approve):
    bar = button_bar(tk, root)
    deny = neon_button(tk, bar, column=0, text='Reject', command=reject)
    neon_button(tk, bar, column=1, text='Approve ONCE', command=approve, primary=True)
    neon_button(tk, bar, column=2, text='10-minute repeat permission (not enabled yet)', disabled=True)
    deny.focus_set()
    return deny
