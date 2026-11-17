"""Host portability for read-only review. No project code is executed."""
import os,stat
from pathlib import Path
from contextlib import contextmanager
from context_manager import InvalidUpdate

@contextmanager
def lock_file(path):
 with Path(path).open('a+b') as stream:
  try:
   if os.name=='nt':
    import msvcrt
    stream.seek(0,2)
    if not stream.tell():stream.write(b'0');stream.flush()
    stream.seek(0);msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
   else:
    import fcntl
    fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
  except OSError:raise InvalidUpdate('A request is already running for this task') from None
  try:yield
  finally:
   if os.name=='nt':
    stream.seek(0);msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)

def windows_read(workspace,name,limit,*,binary=False):
 # Open each component without following reparse points; deny delete sharing so
 # directories cannot be renamed or redirected while the file handle is used.
 import ctypes,msvcrt
 from ctypes import wintypes
 kernel=ctypes.WinDLL('kernel32',use_last_error=True)
 create=kernel.CreateFileW
 create.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE];create.restype=wintypes.HANDLE
 close=kernel.CloseHandle;close.argtypes=[wintypes.HANDLE];close.restype=wintypes.BOOL
 class Info(ctypes.Structure):
  _fields_=[('attributes',wintypes.DWORD),('created',wintypes.FILETIME),('accessed',wintypes.FILETIME),('modified',wintypes.FILETIME),('volume',wintypes.DWORD),('size_hi',wintypes.DWORD),('size_lo',wintypes.DWORD),('links',wintypes.DWORD),('index_hi',wintypes.DWORD),('index_lo',wintypes.DWORD)]
 getinfo=kernel.GetFileInformationByHandle;getinfo.argtypes=[wintypes.HANDLE,ctypes.POINTER(Info)];getinfo.restype=wintypes.BOOL
 handles=[];root=Path(workspace).resolve();parts=[root,*[root.joinpath(*Path(name).parts[:i]) for i in range(1,len(Path(name).parts)+1)]]
 try:
  for i,p in enumerate(parts):
   directory=i<len(parts)-1
   h=create(str(p),0 if directory else 0x80000000,1|2,None,3,0x00200000|(0x02000000 if directory else 0),None)
   if h==ctypes.c_void_p(-1).value:raise OSError(ctypes.get_last_error(),'Cannot open scoped source')
   handles.append(h);info=Info()
   if not getinfo(h,ctypes.byref(info)):raise OSError('Cannot inspect source handle')
   if info.attributes&0x400 or bool(info.attributes&0x10)!=directory:raise InvalidUpdate('Reparse points and non-regular source paths are rejected')
   if not directory:
    if info.links!=1:raise InvalidUpdate('Only regular, unlinked code files are supported')
    size=(info.size_hi << 32) | info.size_lo
    if size>limit:
     from repository_tools import SourceReadLimitExceeded
     raise SourceReadLimitExceeded(f'Source {name} needs {size} bytes; remaining page allocation is {limit} bytes')
  fd=msvcrt.open_osfhandle(handles[-1],os.O_RDONLY|os.O_BINARY);handles.pop()
  with os.fdopen(fd,'rb') as stream:
   before=os.fstat(stream.fileno());raw=stream.read(limit+1);after=os.fstat(stream.fileno())
   if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or len(raw)>limit or (before.st_mtime_ns,before.st_size)!=(after.st_mtime_ns,after.st_size):raise InvalidUpdate('Source changed while reading')
  if not binary:
   if b'\0' in raw:raise InvalidUpdate('Binary file rejected')
   raw.decode('utf-8')
  return raw
 finally:
  for h in reversed(handles):close(h)
