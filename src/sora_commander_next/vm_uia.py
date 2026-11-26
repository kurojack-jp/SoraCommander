"""Read-only UI Automation list of the foreground window. Never reads a control's value.

Method numbers come from UIAutomationClient.h (Windows SDK 10.0.26100.0).
"""

import ctypes as ct
from ctypes import wintypes
import uuid


CLSID_CUIAUTOMATION8 = 'e22ad333-b25f-460c-83d0-0581107395c9'
IID_IUIAUTOMATION2 = '34723aff-0c9d-49d0-9896-7ab52df8cd8a'
CONNECTION_TIMEOUT_MS = 2000
TRANSACTION_TIMEOUT_MS = 4000
SCAN_MAX = 2000
ELEMENTS_MAX = 150
NAME_MAX = 80
TREE_SUBTREE = 7
ELEMENT_MODE_NONE = 0

RELEASE = 2
AUTOMATION_ELEMENT_FROM_HANDLE = 6
AUTOMATION_CONTROL_VIEW = 18
AUTOMATION_CREATE_CACHE = 20
AUTOMATION2_PUT_FOCUS = 59
AUTOMATION2_PUT_CONNECTION = 61
AUTOMATION2_PUT_TRANSACTION = 63
CACHE_ADD_PROPERTY = 3
CACHE_PUT_MODE = 11
ELEMENT_FIND_ALL_CACHED = 8
ARRAY_LENGTH = 3
ARRAY_GET = 4
CACHED_CONTROL_TYPE = 53
CACHED_NAME = 55
CACHED_FOCUS = 58
CACHED_ENABLED = 60
CACHED_PASSWORD = 67
CACHED_OFFSCREEN = 70
CACHED_RECTANGLE = 75
# BoundingRectangle, ControlType, Name, HasKeyboardFocus, IsEnabled, IsPassword, IsOffscreen.
PROPERTIES = (30001, 30003, 30005, 30008, 30010, 30019, 30022)

TYPES = dict(enumerate((
    'button', 'calendar', 'checkbox', 'combobox', 'edit', 'hyperlink', 'image', 'listitem', 'list',
    'menu', 'menubar', 'menuitem', 'progressbar', 'radiobutton', 'scrollbar', 'slider', 'spinner',
    'statusbar', 'tab', 'tabitem', 'text', 'toolbar', 'tooltip', 'tree', 'treeitem', 'custom', 'group',
    'thumb', 'datagrid', 'dataitem', 'document', 'splitbutton', 'window', 'pane', 'header', 'headeritem',
    'table', 'titlebar', 'separator', 'semanticzoom', 'appbar'), 50000))
# Containers and decoration without a name only add noise.
UNNAMED_SKIP = frozenset({'custom', 'group', 'thumb', 'pane', 'separator', 'image', 'text', 'window'})


class UIAError(Exception):
    """A failed UI Automation call with a fixed reason."""


class Rect(ct.Structure):
    _fields_ = [('left', ct.c_int32), ('top', ct.c_int32), ('right', ct.c_int32), ('bottom', ct.c_int32)]


def clean(text):
    return ''.join(char if char.isprintable() else ' ' for char in text[:NAME_MAX]).strip()


def clip(rect, size):
    left, top = max(0, rect[0]), max(0, rect[1])
    right, bottom = min(size[0], rect[2]), min(size[1], rect[3])
    if right <= left or bottom <= top:
        return None
    return left, top, right - left, bottom - top


def select(raw, size):
    """Turn raw (type, name, rect, enabled, focused, password, offscreen) rows into the bounded list."""
    elements = []
    for kind, name, rect, enabled, focused, password, offscreen in raw:
        label = TYPES.get(kind, 'other')
        name = clean(name)
        box = clip(rect, size)
        if offscreen or box is None or (not name and label in UNNAMED_SKIP):
            continue
        if len(elements) == ELEMENTS_MAX:
            return elements, True
        elements.append(dict(type=label, name=name, x=box[0], y=box[1], width=box[2], height=box[3],
                             enabled=bool(enabled), focused=bool(focused), password=bool(password)))
    return elements, False


class _Com:
    """The few raw COM calls that UI Automation needs. Built only when used."""

    def __init__(self):
        self.initialised = False
        self.ole = ct.OleDLL('ole32')
        self.ole.CoUninitialize.argtypes = ()
        self.ole.CoUninitialize.restype = None
        self.oleaut = ct.WinDLL('oleaut32')
        self.user = ct.WinDLL('user32')
        self.ole.CoInitializeEx.argtypes = (ct.c_void_p, wintypes.DWORD)
        self.ole.CoCreateInstance.argtypes = (ct.c_void_p, ct.c_void_p, wintypes.DWORD, ct.c_void_p,
                                              ct.POINTER(ct.c_void_p))
        self.oleaut.SysStringLen.argtypes = self.oleaut.SysFreeString.argtypes = (ct.c_void_p,)
        self.oleaut.SysStringLen.restype = wintypes.UINT
        self.oleaut.SysFreeString.restype = None
        self.user.GetForegroundWindow.argtypes = ()
        self.user.GetForegroundWindow.restype = ct.c_void_p

    def call(self, pointer, index, types, *args, result=ct.c_int32):
        table = ct.cast(pointer, ct.POINTER(ct.POINTER(ct.c_void_p))).contents
        status = ct.WINFUNCTYPE(result, ct.c_void_p, *types)(table[index])(pointer, *args)
        if result is ct.c_int32 and status < 0:
            raise UIAError('UIA_CALL')
        return status

    def out(self, pointer, index, kind, types=(), *args):
        value = kind()
        self.call(pointer, index, (*types, ct.POINTER(kind)), *args, ct.byref(value))
        return value

    def interface(self, pointer, index, types=(), *args):
        value = self.out(pointer, index, ct.c_void_p, types, *args)
        if not value:
            raise UIAError('UIA_EMPTY')
        return value

    def release(self, *pointers):
        for pointer in pointers:
            if pointer:
                self.call(pointer, RELEASE, (), result=ct.c_uint32)

    def text(self, pointer, index):
        value = self.out(pointer, index, ct.c_void_p)
        if not value:
            return ''
        try:
            return ct.wstring_at(value, min(self.oleaut.SysStringLen(value), NAME_MAX))
        finally:
            self.oleaut.SysFreeString(value)

    def automation(self):
        status = ct.WinDLL('ole32').CoInitializeEx(None, 0)
        if status not in (0, 1):  # S_OK, S_FALSE (already initialised)
            raise UIAError('COM_INIT')
        self.initialised = True
        pointer = ct.c_void_p()
        clsid, iid = (ct.create_string_buffer(uuid.UUID(value).bytes_le, 16)
                      for value in (CLSID_CUIAUTOMATION8, IID_IUIAUTOMATION2))
        self.ole.CoCreateInstance(clsid, None, 1, iid, ct.byref(pointer))
        try:
            self.call(pointer, AUTOMATION2_PUT_FOCUS, (ct.c_int32,), 0)
            self.call(pointer, AUTOMATION2_PUT_CONNECTION, (ct.c_uint32,), CONNECTION_TIMEOUT_MS)
            self.call(pointer, AUTOMATION2_PUT_TRANSACTION, (ct.c_uint32,), TRANSACTION_TIMEOUT_MS)
        except BaseException:
            self.release(pointer)
            raise
        return pointer

    def close(self):
        # Every successful CoInitializeEx, S_FALSE included, needs one CoUninitialize.
        if self.initialised:
            self.initialised = False
            self.ole.CoUninitialize()


def _row(com, element):
    rect = com.out(element, CACHED_RECTANGLE, Rect)
    return (com.out(element, CACHED_CONTROL_TYPE, ct.c_int32).value, com.text(element, CACHED_NAME),
            (rect.left, rect.top, rect.right, rect.bottom),
            com.out(element, CACHED_ENABLED, ct.c_int32).value,
            com.out(element, CACHED_FOCUS, ct.c_int32).value,
            com.out(element, CACHED_PASSWORD, ct.c_int32).value,
            com.out(element, CACHED_OFFSCREEN, ct.c_int32).value)


def read_rows(com, window):
    automation = condition = cache = root = found = None
    rows = []
    try:
        automation = com.automation()
        condition = com.interface(automation, AUTOMATION_CONTROL_VIEW)
        cache = com.interface(automation, AUTOMATION_CREATE_CACHE)
        for prop in PROPERTIES:
            com.call(cache, CACHE_ADD_PROPERTY, (ct.c_int32,), prop)
        com.call(cache, CACHE_PUT_MODE, (ct.c_int32,), ELEMENT_MODE_NONE)
        root = com.interface(automation, AUTOMATION_ELEMENT_FROM_HANDLE, (ct.c_void_p,), window)
        found = com.interface(root, ELEMENT_FIND_ALL_CACHED, (ct.c_int32, ct.c_void_p, ct.c_void_p),
                              TREE_SUBTREE, condition, cache)
        length = com.out(found, ARRAY_LENGTH, ct.c_int32).value
        for index in range(min(length, SCAN_MAX)):
            element = com.interface(found, ARRAY_GET, (ct.c_int32,), index)
            try:
                rows.append(_row(com, element))
            finally:
                com.release(element)
        return rows, length > SCAN_MAX
    finally:
        try:
            com.release(found, root, cache, condition, automation)
        finally:
            com.close()


def elements(size, *, com_factory=_Com, window=None):
    """The visible controls of the foreground window, as plain data from an untrusted screen."""
    try:
        com = com_factory()
        window = com.user.GetForegroundWindow() if window is None else window
        if not window:
            return dict(window='', elements=[], truncated=False)
        rows, cut = read_rows(com, window)
    except OSError:
        raise UIAError('UIA_CALL') from None
    items, truncated = select(rows, size)
    return dict(window=clean(rows[0][1]) if rows else '', elements=items, truncated=truncated or cut)
