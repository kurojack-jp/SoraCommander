"""DEV_ONLY BCrypt ECDSA P-256 ephemeral sign/verify, no persisted keys."""
import ctypes as C
import hashlib
import sys
from ctypes import wintypes as W

def _ok(code, label):
    if code != 0:
        raise RuntimeError(f"{label}: NTSTATUS=0x{code & 0xffffffff:08x}")

def sample():
    if sys.platform != "win32":
        raise RuntimeError("Windows required")
    lib = C.WinDLL("bcrypt.dll")
    H = C.c_void_p
    U = C.c_ulong
    B = C.POINTER(C.c_ubyte)
    lib.BCryptOpenAlgorithmProvider.argtypes = [C.POINTER(H), W.LPCWSTR, W.LPCWSTR, U]
    lib.BCryptOpenAlgorithmProvider.restype = C.c_long
    lib.BCryptGenerateKeyPair.argtypes = [H, C.POINTER(H), U, U]
    lib.BCryptGenerateKeyPair.restype = C.c_long
    lib.BCryptFinalizeKeyPair.argtypes = [H, U]
    lib.BCryptFinalizeKeyPair.restype = C.c_long
    lib.BCryptExportKey.argtypes = [H,H,W.LPCWSTR,B,U,C.POINTER(U),U]
    lib.BCryptExportKey.restype = C.c_long
    lib.BCryptSignHash.argtypes = [H,H,B,U,B,U,C.POINTER(U),U]
    lib.BCryptSignHash.restype = C.c_long
    lib.BCryptVerifySignature.argtypes = [H,H,B,U,B,U,U]
    lib.BCryptVerifySignature.restype = C.c_long
    lib.BCryptDestroyKey.argtypes = [H]
    lib.BCryptCloseAlgorithmProvider.argtypes = [H,U]
    algorithm,key=H(),H()
    try:
        _ok(lib.BCryptOpenAlgorithmProvider(C.byref(algorithm),"ECDSA_P256",None,0),"OpenAlgorithm")
        _ok(lib.BCryptGenerateKeyPair(algorithm,C.byref(key),256,0),"Generate")
        _ok(lib.BCryptFinalizeKeyPair(key,0),"Finalize")
        n=U()
        _ok(lib.BCryptExportKey(key,None,"ECCPUBLICBLOB",None,0,C.byref(n),0),"PublicSize")
        pub=(C.c_ubyte*n.value)()
        _ok(lib.BCryptExportKey(key,None,"ECCPUBLICBLOB",pub,len(pub),C.byref(n),0),"ExportPublic")
        message=b"SoraCommander B04 native BCrypt ephemeral v1"
        digest=hashlib.sha256(message).digest()
        h=(C.c_ubyte*len(digest)).from_buffer_copy(digest)
        siglen=U()
        _ok(lib.BCryptSignHash(key,None,h,len(h),None,0,C.byref(siglen),0),"SignatureSize")
        sig=(C.c_ubyte*siglen.value)()
        _ok(lib.BCryptSignHash(key,None,h,len(h),sig,len(sig),C.byref(siglen),0),"SignHash")
        _ok(lib.BCryptVerifySignature(key,None,h,len(h),sig,siglen.value,0),"NativeVerify")
        return bytes(pub),bytes(sig[:siglen.value]),message
    finally:
        if key.value:lib.BCryptDestroyKey(key)
        if algorithm.value:lib.BCryptCloseAlgorithmProvider(algorithm,0)

if __name__=="__main__":
    public,signature,message=sample()
    print("BCrypt ephemeral public blob bytes:",len(public))
    print("P1363 signature bytes:",len(signature))
    print("No private key export or persistence")
