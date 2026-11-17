import unittest

from review_targets import document_input_notice


class DocumentTargetTests(unittest.TestCase):
    def test_resume_request_with_unquoted_local_path(self):
        prompt = (
            "can you review this resume and comment? the person is looking for post silicon "
            "validation opportunities /Users/example/Downloads/Post_Silicon_Resume_ATS (1).docx"
        )
        notice = document_input_notice(prompt)
        self.assertEqual(notice, "Sutra needs the text from this DOCX file. Paste it here to continue the review.")
        self.assertNotIn("/Users/", notice)

    def test_direct_document_targets(self):
        for prompt, kind in (
            ('Critique "Cover Letter (draft 3).DOC".', 'DOC'),
            (r'Review C:\Users\Example\Cover Letter (draft 2).docx, please.', 'DOCX'),
            ('Summarize "/Users/example/Cover Letter (draft 2).pdf".', 'PDF'),
            ('Summarize this `docs/report.pdf` for me.', 'PDF'),
            ('Review the file fixtures/demo.DOCX.', 'DOCX'),
            ('Look at report.pdf.', 'PDF'),
            ('Analyze this draft\nfile:///private/tmp/Report%20draft.pdf', 'PDF'),
        ):
            with self.subTest(prompt=prompt):
                self.assertIn(f"this {kind} file", document_input_notice(prompt))

    def test_general_conversation_and_code_targets_are_untouched(self):
        for prompt in (
            'Resume our architecture review.',
            'Explain how PDF parsing in the repository works.',
            'Review the PDF parser in src/pdf.ts, which uses /tmp/example.pdf as a fixture.',
            'Review src/export.py for how it writes /tmp/example.docx.',
            'Review report.pdf.ts.',
            'Review what public PDF formats support.',
            'Review this report at https://example.com/report.pdf',
            'Review "https://example.com/report.pdf".',
            'Review the code without reading /tmp/draft.docx.',
            'Please do not review /tmp/draft.pdf. Review parser.py instead.',
            'How do I open a .docx?',
        ):
            with self.subTest(prompt=prompt):
                self.assertIsNone(document_input_notice(prompt))
