"""Simple tests for generate_samplesheet.py"""
import pytest
from unittest.mock import patch
from scripts.generate_samplesheet import generate_samplesheet, infer_output_dir


class TestInferOutputDir:
    def test_replaces_raw_with_siz(self):
        input_path = "s3://bucket/delivery/raw/sample_1.fastq.gz"
        expected = "s3://bucket/delivery/siz/"
        assert infer_output_dir(input_path) == expected


class TestGenerateSamplesheet:
    @patch('scripts.generate_samplesheet.list_s3_files')
    def test_generates_pairs(self, mock_list_s3):
        # Mock S3 file listing
        mock_list_s3.side_effect = [
            ['sample1_1.fastq.gz', 'sample1_2.fastq.gz'],  # raw files
            []  # no siz files
        ]

        samples = generate_samplesheet('bucket', 'delivery')

        assert len(samples) == 1
        assert samples[0]['id'] == 'sample1'
        assert samples[0]['fastq_1'] == 's3://bucket/delivery/raw/sample1_1.fastq.gz'
        assert samples[0]['fastq_2'] == 's3://bucket/delivery/raw/sample1_2.fastq.gz'
        assert samples[0]['outdir'] == 's3://bucket/delivery/siz/'

    @patch('scripts.generate_samplesheet.list_s3_files')
    def test_skips_existing_siz_files(self, mock_list_s3):
        mock_list_s3.side_effect = [
            ['sample1_1.fastq.gz', 'sample1_2.fastq.gz',
             'sample2_1.fastq.gz', 'sample2_2.fastq.gz'],
            ['sample1_chunk000000.fastq.zst']  # sample1 already processed
        ]

        samples = generate_samplesheet('bucket', 'delivery')

        assert len(samples) == 1
        assert samples[0]['id'] == 'sample2'

    @patch('scripts.generate_samplesheet.list_s3_files')
    def test_ignore_existing_flag(self, mock_list_s3):
        mock_list_s3.side_effect = [
            ['sample1_1.fastq.gz', 'sample1_2.fastq.gz'],
            ['sample1_chunk000000.fastq.zst']
        ]

        samples = generate_samplesheet('bucket', 'delivery', ignore_existing=True)

        assert len(samples) == 1  # Should include sample1 despite existing siz
        assert samples[0]['id'] == 'sample1'

    @patch('scripts.generate_samplesheet.list_s3_files')
    def test_skips_incomplete_pairs(self, mock_list_s3):
        mock_list_s3.side_effect = [
            ['sample1_1.fastq.gz'],  # Missing _2 file
            []
        ]

        samples = generate_samplesheet('bucket', 'delivery')

        assert len(samples) == 0
