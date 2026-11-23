"""Fingerprints of the code in object files, to tell a mutant that compiles to the original code from one that doesn't.

A mutant whose object files hold the same code and data as the original's is equivalent: no test can detect it, so
running the tests only spends time. The fingerprint leaves out what changes without changing behaviour:

- Debug sections, whose column numbers move when an operator changes length.
- The stamp gcov writes into each object, which is new on every compile.

Anything else that differs makes the objects different, so the comparison errs towards running the tests.
Objects that aren't 64-bit ELF, such as Mach-O or COFF ones, get no fingerprint and every mutant runs its tests.
"""

import hashlib
import mmap
import struct
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

ELF_MAGIC = b"\x7fELF"
ELFCLASS64 = 2
ELFDATA2LSB = 1
ELFDATA2MSB = 2
SHT_SYMTAB = 2
SHT_RELA = 4
SHT_NOBITS = 8
SHT_REL = 9
SHF_WRITE = 0x1
# Sections that describe the code for debuggers and people, not for the machine.
IGNORED_SECTION_PREFIXES = (".debug", ".zdebug", ".rela.debug", ".rel.debug", ".gnu.debuglto_", ".comment")
GCOV_MERGE_PREFIX = b"__gcov_merge_"
# In GCC's 64-bit `struct gcov_info`, `merge[0]` sits 32 bytes in and the 4-byte compile stamp 16 bytes in, with or
# without the checksum field GCC 12 added after it.
GCOV_STAMP_BEFORE_MERGE = 16
GCOV_STAMP_SIZE = 4


@dataclass(frozen=True, slots=True)
class _Section:
    name: str
    type: int
    flags: int
    offset: int
    size: int
    link: int
    info: int
    entry_size: int


class _Elf:
    """Reads sections straight out of the mapped file, since objects with thousands of sections are common."""

    def __init__(self, data: memoryview) -> None:
        self.data = data
        self.order = "<" if data[5] == ELFDATA2LSB else ">"
        section_offset = self.unpack("Q", data, 0x28)
        entry_size, count, names_index = struct.unpack_from(f"{self.order}HHH", data, 0x3A)
        raw = [
            struct.unpack_from(f"{self.order}IIQQQQIIQQ", data, section_offset + index * entry_size)
            for index in range(count)
        ]
        names = bytes(self.read(raw[names_index][4], raw[names_index][5])) if names_index < count else b""
        self.sections = [
            _Section(_string(names, name), kind, flags, offset, size, link, info, entsize)
            for name, kind, flags, _address, offset, size, link, info, _align, entsize in raw
        ]
        self._merge_symbols: dict[int, frozenset[int]] = {}

    def unpack(self, kind: str, data: memoryview, offset: int) -> int:
        return int(struct.unpack_from(f"{self.order}{kind}", data, offset)[0])

    def read(self, offset: int, size: int) -> memoryview:
        if offset + size > len(self.data):
            raise ValueError("section past the end of the file")
        return self.data[offset : offset + size]

    def content(self, section: _Section) -> memoryview:
        return self.data[:0] if section.type == SHT_NOBITS else self.read(section.offset, section.size)

    def merge_symbols(self, index: int) -> frozenset[int]:
        """The indices of gcov's merge functions in symbol table `index`, read once however many sections use it."""
        if index not in self._merge_symbols:
            symtab = self.sections[index]
            data = self.content(symtab)
            strings = bytes(self.content(self.sections[symtab.link])) if symtab.link < len(self.sections) else b""
            size = symtab.entry_size or 24
            offsets = struct.iter_unpack(f"{self.order}I{size - 4}x", data[: len(data) - len(data) % size])
            self._merge_symbols[index] = frozenset(
                number for number, (offset,) in enumerate(offsets) if strings.startswith(GCOV_MERGE_PREFIX, offset)
            )
        return self._merge_symbols[index]

    def relocates_writable_data(self, section: _Section) -> bool:
        if section.type not in (SHT_RELA, SHT_REL) or section.link >= len(self.sections):
            return False
        return section.info < len(self.sections) and bool(self.sections[section.info].flags & SHF_WRITE)

    def gcov_stamps(self) -> dict[int, int]:
        """The offset of the gcov stamp in each section that holds a `struct gcov_info`, keyed by section index.

        `struct gcov_info` is writable data, so only the relocations of writable sections are read: an object built
        with -ffunction-sections or many templates has thousands of relocation sections for its code.
        """
        stamps: dict[int, int] = {}
        for section in self.sections:
            if not self.relocates_writable_data(section):
                continue
            merge_symbols = self.merge_symbols(section.link)
            if not merge_symbols:
                continue
            data = self.content(section)
            size = section.entry_size or (24 if section.type == SHT_RELA else 16)
            entries = struct.iter_unpack(f"{self.order}QQ{size - 16}x", data[: len(data) - len(data) % size])
            merges = [offset for offset, info in entries if info >> 32 in merge_symbols]
            if merges and min(merges) >= GCOV_STAMP_BEFORE_MERGE:
                stamps[section.info] = min(merges) - GCOV_STAMP_BEFORE_MERGE
        return stamps


def _string(table: bytes, offset: int) -> str:
    return _cstring(table, offset).decode("utf-8", "replace")


def _cstring(table: bytes, offset: int) -> bytes:
    end = table.find(b"\0", offset)
    return table[offset : len(table) if end < 0 else end]


def _masked(content: memoryview, stamp: int | None) -> memoryview | bytes:
    if stamp is None or stamp + GCOV_STAMP_SIZE > len(content):
        return content
    return bytes(content[:stamp]) + bytes(GCOV_STAMP_SIZE) + bytes(content[stamp + GCOV_STAMP_SIZE :])


def _fingerprint(data: memoryview) -> str | None:
    if len(data) < 64 or bytes(data[:4]) != ELF_MAGIC or data[4] != ELFCLASS64:
        return None
    if data[5] not in (ELFDATA2LSB, ELFDATA2MSB):
        return None
    elf = _Elf(data)
    stamps = elf.gcov_stamps()
    digest = hashlib.sha256()
    for index, section in enumerate(elf.sections):
        if section.name.startswith(IGNORED_SECTION_PREFIXES):
            continue
        content = _masked(elf.content(section), stamps.get(index))
        digest.update(f"{section.name}\0{section.type}\0{len(content)}\0".encode())
        digest.update(content)
    return digest.hexdigest()


def object_fingerprint(path: Path) -> str | None:
    """A hash of the code and data in the object at `path`; None when it is missing or not a 64-bit ELF object."""
    try:
        with path.open("rb") as file, mmap.mmap(file.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
            with memoryview(mapped) as data:
                return _fingerprint(data)
    except (OSError, struct.error, IndexError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class FileStamp:
    """What changes when a build writes a file again."""

    mtime_ns: int
    size: int
    inode: int


def file_stamp(path: Path) -> FileStamp | None:
    try:
        status = path.stat()
    except OSError:
        return None
    return FileStamp(status.st_mtime_ns, status.st_size, status.st_ino)


class ObjectFingerprints:
    """The fingerprint of every object file of the unmutated build, and which objects each build writes again.

    Take the originals right after building the unmutated sources. Before each mutant's build, `snapshot` records the
    objects as they are; after it, `rebuilt` names the objects the build wrote again.
    """

    def __init__(self, originals: Mapping[Path, str | None]) -> None:
        self.originals = dict(originals)
        self._before: dict[Path, FileStamp | None] = {}

    @classmethod
    def of(cls, objects: Iterable[Path]) -> "ObjectFingerprints":
        return cls({path: object_fingerprint(path) for path in dict.fromkeys(objects)})

    def snapshot(self) -> None:
        self._before = {path: file_stamp(path) for path in self.originals}

    def rebuilt(self) -> list[Path]:
        return [path for path, before in self._before.items() if file_stamp(path) != before]

    def all_original(self, objects: Iterable[Path]) -> bool:
        """Whether every object in `objects` holds the same code and data as the unmutated build's."""
        for path in objects:
            original = self.originals.get(path)
            if original is None or object_fingerprint(path) != original:
                return False
        return True
