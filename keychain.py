"""Store provider API keys in the macOS login Keychain."""

import ctypes as ct
import sys


class Keychain:
    SERVICE = b"KinectProjection model API"
    NOT_FOUND = -25300
    DUPLICATE = -25299

    def __init__(self):
        if sys.platform != "darwin":
            raise OSError("Saving an API key requires macOS Keychain. Set WALL_LLM_KEY in the environment instead.")
        frameworks = "/System/Library/Frameworks"
        security = ct.CDLL(f"{frameworks}/Security.framework/Security")
        core = ct.CDLL(f"{frameworks}/CoreFoundation.framework/CoreFoundation")
        ref, size, string = ct.c_void_p, ct.c_uint32, ct.c_char_p
        pointer = ct.POINTER
        self.find = security.SecKeychainFindGenericPassword
        self.find.argtypes = [ref, size, string, size, string, pointer(size), pointer(ref), pointer(ref)]
        self.add = security.SecKeychainAddGenericPassword
        self.add.argtypes = [ref, size, string, size, string, size, ref, pointer(ref)]
        self.modify = security.SecKeychainItemModifyAttributesAndData
        self.modify.argtypes = [ref, ref, size, ref]
        self.free = security.SecKeychainItemFreeContent
        self.free.argtypes = [ref, ref]
        self.delete = security.SecKeychainItemDelete
        self.delete.argtypes = [ref]
        self.release = core.CFRelease
        self.release.argtypes = [ref]
        self.release.restype = None

    def _location(self, url):
        account = url.encode("utf-8")
        return None, len(self.SERVICE), self.SERVICE, len(account), account

    @staticmethod
    def _check(status):
        if status:
            raise OSError(f"macOS Keychain returned {status}. Unlock or allow access in Keychain Access, or use an environment key.")

    def read(self, url):
        length, value = ct.c_uint32(), ct.c_void_p()
        code = self.find(*self._location(url), ct.byref(length), ct.byref(value), None)
        if code == self.NOT_FOUND:
            return ""
        self._check(code)
        try:
            return ct.string_at(value, length.value).decode("utf-8")
        finally:
            self.free(None, value)

    def save(self, url, key):
        data = key.encode("utf-8")
        buffer = ct.c_char_p(data)
        code = self.add(*self._location(url), len(data), buffer, None)
        if code != self.DUPLICATE:
            self._check(code)
            return
        item = ct.c_void_p()
        self._check(self.find(*self._location(url), None, None, ct.byref(item)))
        try:
            self._check(self.modify(item, None, len(data), buffer))
        finally:
            self.release(item)

    def forget(self, url):
        item = ct.c_void_p()
        code = self.find(*self._location(url), None, None, ct.byref(item))
        if code == self.NOT_FOUND:
            return
        self._check(code)
        try:
            self._check(self.delete(item))
        finally:
            self.release(item)
