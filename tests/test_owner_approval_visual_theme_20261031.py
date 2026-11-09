"""Isolated presentation tests: fake Tk only, no display, sound or command."""
import ast
import builtins
import io
import json
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from sora_commander_next import approval_visual_theme as theme
from sora_commander_next import file_edits as edits
from sora_commander_next import owner_approval_dialog as dialog


def fake_tk():
    tk = Mock()
    tk.Tk.return_value.winfo_screenwidth.return_value = 1920
    tk.Tk.return_value.winfo_screenheight.return_value = 1080
    return tk


class ApprovalVisualTests(unittest.TestCase):
    def test_import_has_no_tk_or_display_side_effects(self):
        real_import = builtins.__import__
        def safe_import(name, *args, **kwargs):
            if name in ('tkinter', 'winsound'):
                self.fail('UI/audio imported during module import')
            return real_import(name, *args, **kwargs)
        with patch('builtins.__import__', side_effect=safe_import):
            for module in (theme, dialog):
                runpy.run_path(module.__file__, run_name='isolated_import_test')

    def test_fixed_bundled_icon_paths_and_instance_reference(self):
        self.assertEqual(theme.ICON_PNG, ROOT / 'branding' / 'plugin-icon.png')
        self.assertEqual(theme.ICON_ICO, ROOT / 'branding' / 'SoraCommander.ico')
        self.assertTrue(theme.ICON_PNG.is_file())
        self.assertTrue(theme.ICON_ICO.is_file())
        tk = fake_tk()
        root = tk.Tk()
        theme.header(tk, root, 'Owner review')
        self.assertEqual(tk.PhotoImage.call_args_list[0].kwargs, {'master': root, 'file': str(theme.ICON_PNG)})
        self.assertIs(root._sora_review_image, tk.PhotoImage.return_value)
        root.iconbitmap.assert_called_once_with(str(theme.ICON_ICO))

    def test_missing_or_invalid_icons_do_not_block_review(self):
        tk = fake_tk()
        tk.PhotoImage.side_effect = ValueError('invalid image')
        root = tk.Tk()
        root.iconbitmap.side_effect = OSError('missing icon')
        theme.header(tk, root, 'Owner review')
        self.assertTrue(tk.Label.called)

    def test_palette_contrast_and_button_emphasis(self):
        def luminance(color):
            rgb = [int(color[i:i+2], 16) / 255 for i in (1, 3, 5)]
            linear = [c / 12.92 if c <= .04045 else ((c + .055) / 1.055) ** 2.4 for c in rgb]
            return sum(c * w for c, w in zip(linear, (.2126, .7152, .0722)))
        for foreground, background in (('text', 'viewer'), ('text', 'blue'),
                                        ('amber', 'caution'), ('secondary', 'info')):
            ratio = (luminance(theme.COLORS[foreground]) + .05) / (luminance(theme.COLORS[background]) + .05)
            self.assertGreaterEqual(ratio, 4.5)
        self.assertEqual(theme.COLORS['background'], '#071426')
        self.assertEqual(theme.button_options(primary=True)['background'], theme.COLORS['blue'])
        self.assertEqual(theme.button_options()['background'], theme.COLORS['muted'])

    def test_screen_bounds_resize_and_two_axis_viewer(self):
        tk = fake_tk()
        root = tk.Tk()
        theme.window(root)
        root.geometry.assert_called_once_with('1536x900')
        root.resizable.assert_called_once_with(True, True)
        root.winfo_screenwidth.return_value = 800
        root.winfo_screenheight.return_value = 600
        theme.window(root)
        self.assertEqual(root.geometry.call_args.args, ('720x500',))
        pane = theme.viewer(tk, root)
        self.assertEqual(tk.Text.call_args.kwargs['wrap'], 'none')
        self.assertFalse(tk.Text.call_args.kwargs['exportselection'])
        self.assertEqual(tk.Canvas.call_count, 2)
        tk.Scrollbar.assert_not_called()
        self.assertIn('xscrollcommand', pane.configure.call_args.kwargs)
        self.assertIn('yscrollcommand', pane.configure.call_args.kwargs)

    def command_review(self, action):
        tk = fake_tk()
        root = tk.Tk()
        request = dict(nonce='a' * 64, executable='C:/exact app/python.exe',
                       argv=['x' * 12000, '\t\n\\"', '日本語'], cwd='C:/exact cwd',
                       pending_id='review-123', seconds=9, timeout=12, output_limit=8192)
        timers = []
        root.after.side_effect = lambda delay, callback: timers.append((delay, callback))
        def loop():
            if action in ('Approve ONCE', 'Reject'):
                next(c.kwargs['command'] for c in tk.Button.call_args_list
                     if c.kwargs['text'] == action)()
            elif action == 'close':
                root.protocol.call_args.args[1]()
            elif action == 'timeout':
                next(callback for delay, callback in timers if delay == 9000)()
            elif action == 'error':
                raise RuntimeError('synthetic UI failure')
        root.mainloop.side_effect = loop
        output = io.BytesIO()
        with patch.dict(sys.modules, {'tkinter': tk}), \
                patch.object(dialog.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(json.dumps(request).encode() + b'\n'))), \
                patch.object(dialog.sys, 'stdout', SimpleNamespace(buffer=output)):
            if action == 'error':
                with self.assertRaises(RuntimeError):
                    dialog.main()
                self.assertEqual(output.getvalue(), b'')
                return
            dialog.main()
        response = json.loads(output.getvalue())
        self.assertEqual(set(response), {'nonce', 'decision'})
        self.assertEqual(response['nonce'], request['nonce'])
        self.assertEqual([delay for delay, _ in timers], [9000, 100])
        tk.Tk.return_value.bell.assert_not_called()
        text = tk.Text.return_value.insert.call_args.args[1]
        for field in ('executable', 'argv', 'cwd', 'pending_id'):
            self.assertIn(repr(request[field]), text)
        self.assertIn('Timeout: 12 s\nOutput limit: 8192 bytes', text)
        self.assertEqual(tk.Text.return_value.configure.call_args.kwargs['state'], 'disabled')
        labels = [c.kwargs.get('text', '') for c in tk.Label.call_args_list]
        self.assertIn('Read the ENTIRE command before clicking Approve. Close = DENY.', labels)
        self.assertIn('時間切れは却下ではありません。操作は実行せず、次の指示を待ちます。 Timeout != Reject.', labels)
        repeat = next(c for c in tk.Button.call_args_list if '10-minute' in c.kwargs['text'])
        self.assertEqual(repeat.kwargs['state'], 'disabled')
        self.assertNotIn('command', repeat.kwargs)
        return response['decision']

    def test_command_buttons_close_and_no_click_keep_protocol(self):
        for action, expected in (('Approve ONCE', 'APPROVE'), ('Reject', 'DENY'),
                                 ('close', 'DENY'), ('timeout', 'EXPIRED'), ('no click', 'EXPIRED')):
            with self.subTest(action=action):
                self.assertEqual(self.command_review(action), expected)

    def test_command_ui_failure_emits_no_approval(self):
        self.command_review('error')

    def test_visual_rail_is_separate_from_payload_at_desktop_sizes(self):
        for screen, geometry in (((1366, 768), '1286x668'), ((1920, 1080), '1536x900')):
            tk = fake_tk()
            root = tk.Tk()
            root.winfo_screenwidth.return_value, root.winfo_screenheight.return_value = screen
            theme.window(root)
            root.geometry.assert_called_once_with(geometry)
            main = theme.header(tk, root, 'LOCAL file edit')
            self.assertEqual(tk.Canvas.call_args.kwargs['width'], 220)
            self.assertIsNot(main, root)
            theme.viewer(tk, main)
            self.assertIs(tk.Frame.call_args_list[-2].args[0], main)
            self.assertEqual(tk.Text.call_args.kwargs['height'], 5)

    def test_dark_scrollbar_range_drag_page_and_keyboard(self):
        for vertical in (True, False):
            tk = fake_tk()
            canvas = tk.Canvas.return_value
            canvas.winfo_height.return_value = 100
            canvas.winfo_width.return_value = 100
            command = Mock()
            bar = theme._scrollbar(tk, None, command, vertical=vertical)
            bar.set('.2', '.6')
            bindings = {c.args[0]: c.args[1] for c in bar.bind.call_args_list}
            bindings['<Button-1>'](SimpleNamespace(x=30, y=30))
            bindings['<B1-Motion>'](SimpleNamespace(x=50, y=50))
            self.assertEqual(command.call_args.args[0], 'moveto')
            self.assertAlmostEqual(command.call_args.args[1], .4)
            bindings['<Button-1>'](SimpleNamespace(x=90, y=90))
            command.assert_called_with('scroll', 1, 'pages')
            for key, expected in (('Up', ('scroll', -1, 'units')),
                                  ('Next', ('scroll', 1, 'pages')),
                                  ('Home', ('moveto', 0)), ('End', ('moveto', 1))):
                bindings['<'+key+'>'](None)
                command.assert_called_with(*expected)

    def test_rail_paints_background_behind_branding(self):
        tk = fake_tk()
        theme.header(tk, tk.Tk(), 'command')
        paint = tk.Canvas.return_value.bind.call_args.args[1]
        paint(SimpleNamespace(width=280, height=640))
        self.assertEqual(tk.Canvas.return_value.create_rectangle.call_count, 160)
        self.assertEqual(tk.Canvas.return_value.create_arc.call_count, 4)
        tk.Canvas.return_value.create_image.assert_called_once()
        texts = [c.kwargs.get('text', '') for c in tk.Canvas.return_value.create_text.call_args_list]
        self.assertIn('Your AI Partner.\nMore Freedom.\nMore Possibilities.', texts)
        self.assertTrue(any('LOCAL ONLY' in text for text in texts))

    def test_native_button_focus_enter_space_and_disabled_contract(self):
        for disabled in (False, True):
            tk = fake_tk()
            command = Mock()
            button = theme.neon_button(tk, None, column=1, text='Approve ONCE',
                                       command=command, primary=True, disabled=disabled)
            canvas = tk.Canvas.return_value
            canvas.winfo_width.return_value = 250
            canvas.winfo_height.return_value = 104
            bindings = {c.args[0]: c.args[1] for c in button.bind.call_args_list}
            # A Tk Button class owns Space and pointer activation. Our bindings
            # only paint; returning None must let native class bindings run.
            for event in ('<ButtonPress-1>', '<ButtonRelease-1>', '<KeyPress-space>', '<KeyRelease-space>'):
                self.assertIsNone(bindings[event](None))
            command.assert_not_called()
            button.invoke.assert_not_called()
            button.focus_get.return_value = None
            self.assertEqual(bindings['<Return>'](None), 'break')
            button.invoke.assert_not_called()
            button.focus_get.return_value = button
            button.cget.return_value = 'disabled' if disabled else 'normal'
            bindings['<FocusIn>'](None)
            self.assertTrue(canvas.create_line.called)
            self.assertEqual(bindings['<Return>'](None), 'break')
            self.assertEqual(button.invoke.call_count, 0 if disabled else 1)
            button.cget.return_value = 'disabled'
            bindings['<Return>'](None)
            self.assertEqual(button.invoke.call_count, 0 if disabled else 1)
            kwargs = tk.Button.call_args.kwargs
            self.assertEqual(kwargs['state'], 'disabled' if disabled else 'normal')
            self.assertEqual(kwargs['takefocus'], not disabled)
            self.assertEqual('command' in kwargs, not disabled)
            self.assertEqual([c.args[0] for c in canvas.bind.call_args_list], ['<Configure>'])

    def test_three_columns_reject_focus_and_controls_reserved_before_viewer(self):
        tk = fake_tk()
        root = tk.Tk()
        theme.viewer(tk, root)
        theme.review_actions(tk, root, Mock(), Mock())
        calls = tk.Button.call_args_list
        self.assertEqual([c.kwargs['text'] for c in calls[:2]], ['Reject', 'Approve ONCE'])
        self.assertEqual(calls[2].kwargs['state'], 'disabled')
        self.assertNotIn('command', calls[2].kwargs)
        self.assertTrue(any(c.kwargs.get('before') is root._sora_viewer_panel
                            and c.kwargs.get('side') == 'bottom'
                            for c in tk.Frame.return_value.pack.call_args_list))
        self.assertEqual([c.kwargs['column'] for c in tk.Canvas.return_value.grid.call_args_list[-3:]], [0, 1, 2])
        tk.Button.return_value.focus_set.assert_called_once()

    def test_optional_fixed_portrait_and_failed_decode_fallback(self):
        self.assertEqual(theme.PORTRAIT_PNG, ROOT / 'branding' / 'owner-review-portrait-256.png')
        for valid in (False, True):
            tk = fake_tk()
            logo, portrait = Mock(), Mock()
            tk.PhotoImage.side_effect = [logo, portrait if valid else ValueError('invalid portrait')]
            with patch.object(Path, 'is_file', return_value=True):
                theme.header(tk, tk.Tk(), 'review')
            self.assertEqual(tk.PhotoImage.call_args.kwargs['file'], str(theme.PORTRAIT_PNG))
            self.assertIs(tk.Tk()._sora_review_art[0], logo)
            self.assertIs(tk.Tk()._sora_review_art[1], portrait if valid else None)

    def test_edit_exact_proposal_and_boolean_decisions(self):
        proposal = json.dumps({'before': 'old\r\n', 'after': 'a' * 40000 + '\x00\n日本語'}, ensure_ascii=True)
        for complete in (None, proposal):
            for action, expected in (('Approve ONCE', True), ('Reject', False),
                                     ('close', False), ('timeout', False)):
                with self.subTest(complete=complete is not None, action=action):
                    tk = fake_tk()
                    root = tk.Tk()
                    timers = []
                    root.after.side_effect = lambda delay, cb: timers.append((delay, cb))
                    def loop():
                        if action == 'close':
                            root.protocol.call_args.args[1]()
                        elif action == 'timeout':
                            # The expiry timer carries the remaining 45s budget.
                            delay, cb = max(timers, key=lambda timer: timer[0])
                            self.assertTrue(44000 < delay <= 45000, delay)
                            cb()
                        else:
                            next(c.kwargs['command'] for c in tk.Button.call_args_list
                                 if c.kwargs['text'] == action)()
                    root.mainloop.side_effect = loop
                    with patch.dict(sys.modules, {'tkinter': tk}):
                        result = edits._show_edit_review({'path': 'local review', 'diff': proposal}, complete_review=complete)
                    self.assertIs(result, expected)
                    self.assertEqual(tk.Text.return_value.insert.call_args.args, ('end', proposal))
                    root.destroy.assert_called_once()
                    self.assertFalse(any('EXPIRED' in c.kwargs.get('text', '') for c in tk.Label.call_args_list))

    def test_edit_theme_failure_never_returns_consent(self):
        tk = fake_tk()
        with patch.dict(sys.modules, {'tkinter': tk}), patch.object(theme, 'viewer', side_effect=RuntimeError('synthetic')):
            with self.assertRaises(RuntimeError):
                edits._show_edit_review({'path': 'local', 'diff': 'exact'})
        tk.Tk.return_value.destroy.assert_called_once()
        tk.Tk.return_value.mainloop.assert_not_called()

    def test_theme_has_no_authority_or_process_calls(self):
        tree = ast.parse(Path(theme.__file__).read_text(encoding='utf-8'))
        imports = [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
        self.assertEqual(imports, ['pathlib'])
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotIn(node.func.attr, ('Tk', 'mainloop', 'after', 'approve_local', 'Popen', 'run', 'write_text'))


if __name__ == '__main__':
    unittest.main()
