import io
import unittest
import warnings
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZipFile

import document_text
from context_manager import InvalidUpdate
from document_text import extract_docx


WORD = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
STRICT_WORD = 'http://purl.oclc.org/ooxml/wordprocessingml/main'


def word_xml(body, namespace=WORD):
    return f'<w:document xmlns:w="{namespace}"><w:body>{body}</w:body></w:document>'


def archive(*entries):
    raw = io.BytesIO()
    with ZipFile(raw, 'w', ZIP_DEFLATED) as document:
        for name, content in entries:
            document.writestr(name, content)
    return raw.getvalue()


def docx(xml):
    return archive(('word/document.xml', xml))


class DocumentTextTests(unittest.TestCase):
    def test_extracts_document_order_runs_tables_and_separators(self):
        xml = word_xml('''
            <w:p><w:r><w:t xml:space="preserve">Post-Silicon </w:t></w:r>
              <w:hyperlink><w:r><w:t>Validation</w:t></w:r></w:hyperlink></w:p>
            <w:tbl><w:tr>
              <w:tc><w:p><w:r><w:t>2024–2026</w:t></w:r></w:p></w:tc>
              <w:tc><w:p><w:r><w:t>PCIe</w:t><w:tab/><w:t>Python</w:t>
                <w:br/><w:t>Register automation</w:t></w:r></w:p></w:tc>
            </w:tr></w:tbl>
            <w:p><w:r><w:instrText>INCLUDETEXT ignored</w:instrText>
              <w:delText>Deleted data</w:delText><w:t>Reduced triage time by 17%</w:t></w:r></w:p>
        ''')
        data = archive(('word/document.xml', xml), ('word/header1.xml', 'Header outside the body'),
                       ('word/_rels/document.xml.rels', 'https://example.invalid/document'))
        self.assertEqual(extract_docx(data),
                         'Post-Silicon Validation\n2024–2026\nPCIe\tPython\nRegister automation\nReduced triage time by 17%')

    def test_reads_strict_namespace_and_utf16(self):
        xml = word_xml('<w:p><w:r><w:t>Résumé</w:t></w:r></w:p>', STRICT_WORD)
        self.assertEqual(extract_docx(docx(xml.encode('utf-16'))), 'Résumé')

    def test_rejects_xml_dtd_including_utf16(self):
        xml = '<!DOCTYPE w:document [<!ENTITY private SYSTEM "file:///etc/passwd">]>' + word_xml(
            '<w:p><w:r><w:t>&private;</w:t></w:r></w:p>')
        for encoding in ('utf-8', 'utf-16'):
            with self.subTest(encoding=encoding), self.assertRaisesRegex(InvalidUpdate, 'document type declaration'):
                extract_docx(docx(xml.encode(encoding)))

    def test_rejects_invalid_packages_or_empty_text(self):
        cases = (
            (b'plain text', 'DOCX archive'),
            (archive(('word/header1.xml', '<header/>')), 'main document part'),
            (docx('<w:document>'), 'parse the Word document XML'),
            (docx('<document><body>text</body></document>'), 'recognized document body'),
            (docx(word_xml('<w:p><w:r><w:t>  </w:t></w:r></w:p>')), 'no readable body text'),
        )
        for raw, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(InvalidUpdate, message):
                extract_docx(raw)

    def test_rejects_duplicate_main_parts(self):
        xml = word_xml('<w:p><w:r><w:t>Reviewed text</w:t></w:r></w:p>')
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', UserWarning)
            data = archive(('word/document.xml', xml), ('word/document.xml', xml))
        with self.assertRaisesRegex(InvalidUpdate, 'exactly one main document part'):
            extract_docx(data)

    def test_enforces_archive_xml_and_utf8_text_limits(self):
        data = docx(word_xml('<w:p><w:r><w:t>ééé</w:t></w:r></w:p>'))
        for limit, value, message in (
            ('MAX_DOCX_BYTES', len(data) - 1, 'DOCX read limit'),
            ('MAX_DOCX_XML_BYTES', 32, 'XML is too large'),
            ('MAX_DOCX_TEXT_BYTES', 5, 'text is too large'),
        ):
            with self.subTest(limit=limit), patch.object(document_text, limit, value):
                with self.assertRaisesRegex(InvalidUpdate, message):
                    extract_docx(data)
