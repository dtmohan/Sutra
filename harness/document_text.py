"""Extract bounded Word document text from bytes supplied by the host."""

from io import BytesIO
from xml.etree import ElementTree
from zipfile import BadZipFile, ZIP_DEFLATED, ZIP_STORED, ZipFile
from zlib import error as CompressionError

from context_manager import InvalidUpdate


MAX_DOCX_BYTES = 4 * 1024 * 1024
MAX_DOCX_XML_BYTES = 2 * 1024 * 1024
MAX_DOCX_TEXT_BYTES = 24 * 1024
_WORD_NAMESPACES = (
    'http://schemas.openxmlformats.org/wordprocessingml/2006/main',
    'http://purl.oclc.org/ooxml/wordprocessingml/main',
)


class _WordTree(ElementTree.TreeBuilder):
    def doctype(self, name, pubid, system):
        raise InvalidUpdate('Word document XML must not contain a document type declaration')


def _document_xml(raw):
    try:
        with ZipFile(BytesIO(raw)) as archive:
            entries = [item for item in archive.infolist() if item.filename == 'word/document.xml']
            if len(entries) != 1:
                raise InvalidUpdate('Word file must contain exactly one main document part')
            entry = entries[0]
            if entry.flag_bits & 1 or entry.compress_type not in (ZIP_STORED, ZIP_DEFLATED):
                raise InvalidUpdate('Encrypted or unsupported Word document compression')
            if entry.file_size > MAX_DOCX_XML_BYTES:
                raise InvalidUpdate('Word document XML is too large to read. Paste the relevant section instead.')
            with archive.open(entry) as stream:
                xml = stream.read(MAX_DOCX_XML_BYTES + 1)
            if len(xml) != entry.file_size:
                raise InvalidUpdate('Word document XML size does not match the archive')
            return xml
    except (BadZipFile, CompressionError, EOFError, OSError, RuntimeError, ValueError) as exc:
        raise InvalidUpdate('Could not read the DOCX archive') from exc


def _paragraph_text(paragraph, word):
    pending = list(reversed(paragraph))
    parts = []
    while pending:
        child = pending.pop()
        # A nested paragraph, such as text in a drawing, is read separately.
        if child.tag == word + 'p':
            continue
        if child.tag == word + 't' and child.text:
            parts.append(child.text)
        elif child.tag == word + 'tab':
            parts.append('\t')
        elif child.tag in (word + 'br', word + 'cr'):
            parts.append('\n')
        pending.extend(reversed(child))
    return ''.join(parts).strip()


def extract_docx(raw: bytes) -> str:
    """Return body text in paragraph order, including paragraphs in tables."""
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_DOCX_BYTES:
        raise InvalidUpdate('Word file is empty or exceeds the 4 MiB DOCX read limit')
    xml = _document_xml(raw)
    try:
        root = ElementTree.fromstring(xml, parser=ElementTree.XMLParser(target=_WordTree()))
    except (ElementTree.ParseError, LookupError, ValueError) as exc:
        raise InvalidUpdate('Could not parse the Word document XML') from exc

    word = next(('{' + ns + '}' for ns in _WORD_NAMESPACES if root.tag == '{' + ns + '}document'), None)
    body = root.find(word + 'body') if word else None
    if body is None:
        raise InvalidUpdate('Word file does not contain a recognized document body')

    paragraphs = []
    text_size = 0
    for paragraph in body.iter(word + 'p'):
        text = _paragraph_text(paragraph, word)
        if text:
            text_size += len(text.encode('utf-8')) + bool(paragraphs)
            if text_size > MAX_DOCX_TEXT_BYTES:
                raise InvalidUpdate('Word document text is too large to review at once. Paste the relevant section instead.')
            paragraphs.append(text)
    if not paragraphs:
        raise InvalidUpdate('Word document has no readable body text')
    return '\n'.join(paragraphs)
