import unittest

from review_targets import DOCUMENT_TEXT_REQUIRED, document_input_notice, document_review_requested, has_document_text


class DocumentTargetTests(unittest.TestCase):
    def test_resume_request_with_unquoted_local_path(self):
        prompt = (
            "can you review this resume and comment? the person is looking for post silicon "
            "validation opportunities /Users/example/Downloads/Post_Silicon_Resume_ATS (1).docx"
        )
        notice = document_input_notice(prompt)
        self.assertIn("Sutra needs the text from this DOCX file.", notice)
        self.assertIn("Document text:", notice)
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
            'Please resume review.',
            'Please continue.',
            'Review resume.py.',
            'Review the resume command on this branch.',
            'Review the résumé parsers in resume.py.',
            'Could you review the CV generation library?',
            'I asked it to review my résumé. Now please review the workspace.',
            'Please do not review my résumé. Review the workspace instead.',
            'How do I review my résumé?',
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
                self.assertFalse(document_review_requested(prompt))

    def test_document_review_without_a_filename_requests_text(self):
        for prompt in (
            'i was requesting for the resume review but you gave me worksapce review. '
            'can you look at the resume now please?',
            'Please review my résumé.',
            'Could you provide a CV review?',
            'Can you look at this cover letter?',
            'Document text:\n   ',
            'Résumé text:\n"/tmp/Candidate Resume (1).docx"',
        ):
            with self.subTest(prompt=prompt):
                self.assertTrue(document_review_requested(prompt))
                self.assertFalse(has_document_text(prompt))
                self.assertEqual(document_input_notice(prompt), DOCUMENT_TEXT_REQUIRED)

    def test_explicit_document_text_satisfies_the_input_requirement(self):
        for prompt in (
            'Résumé text:\nValidation engineer. Automated board bring-up.',
            'Review my resume.\nDocument text:\nValidation engineer with Python and lab experience.',
            'Review Candidate.docx.\nCV text:\nSummary\nDeveloped automated silicon tests.',
            'Cover letter text:\r\nI am applying for the validation engineering role.',
        ):
            with self.subTest(prompt=prompt):
                self.assertTrue(has_document_text(prompt))
                self.assertTrue(document_review_requested(prompt))
                self.assertIsNone(document_input_notice(prompt))
