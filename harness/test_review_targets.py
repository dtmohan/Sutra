import unittest

from review_targets import (
    DOCUMENT_TEXT_REQUIRED, document_file_target, document_input_notice,
    document_review_requested, has_document_text,
)


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

    def test_opinion_requests_with_explicit_file_targets(self):
        for prompt, path in (
            ('what is your opinon on this document? - /Users/example/Downloads/remnant.docx',
             '/Users/example/Downloads/remnant.docx'),
            ('What’s your opinion of this report? - "drafts/Review draft.docx"', 'drafts/Review draft.docx'),
            ('Thoughts on this document? - remnant.docx', 'remnant.docx'),
            ('What do you think of this document?\nReview draft.docx', 'Review draft.docx'),
            ('Review this resume please.\nPost_Silicon_Resume_ATS (1).docx\nSuggest revisions.',
             'Post_Silicon_Resume_ATS (1).docx'),
        ):
            with self.subTest(prompt=prompt):
                self.assertTrue(document_review_requested(prompt))
                self.assertIn("this DOCX file", document_input_notice(prompt))
                self.assertEqual(document_file_target(prompt), path)

    def test_admitted_document_intent_can_recover_an_explicit_filename(self):
        prompt = 'Any red flags here? /Users/example/Downloads/remnant.docx'
        self.assertIsNone(document_file_target(prompt))
        self.assertEqual(document_file_target(prompt, classified=True), '/Users/example/Downloads/remnant.docx')

    def test_conflicting_or_negated_references_have_no_automatic_target(self):
        for prompt in (
            'What is your opinion on this document? /tmp/a.docx or /tmp/b.docx',
            'What is your opinion on this document? draft.docx or sample.pdf',
            'Review this resume.\nFirst draft.docx\nSecond draft.docx',
            'Do not review /tmp/draft.docx.',
            'Do not open flags.docx.',
            "What is your opinion on this document? Don't open /tmp/draft.docx.",
            "Thoughts on this document?\nDon't open Second draft (1).docx",
            'Review the code without reading /tmp/sample.docx.',
            'Review Candidate.docx.\nDocument text:\nThis excerpt is already available.',
        ):
            with self.subTest(prompt=prompt):
                self.assertIsNone(document_file_target(prompt))
                self.assertIsNone(document_file_target(prompt, classified=True))

    def test_negation_of_other_files_preserves_selected_target(self):
        prompt = "What is your opinion on this document? - /tmp/selected.docx. Don't open /tmp/other.docx."
        self.assertEqual(document_file_target(prompt), '/tmp/selected.docx')

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
            'What is your opinion of this document parser? /tmp/example.docx is a fixture.',
            'What is your opinion on the PDF readers in the repo? /tmp/example.docx is a fixture.',
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
                self.assertIsNone(document_file_target(prompt))

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
