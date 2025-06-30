// Process to generate the sample sheet from S3 listings using 'scripts/generate_samplesheet.py'
process GENERATE_SAMPLESHEET {
    tag "${params.bucket}/${params.delivery}"
  
  input:
    // Receive bucket and delivery as values
    val bucket
    val delivery
    val outdir
    val ignore_existing
    path script
  
  output:
    // Produce a sample_sheet.csv file in the process workDir
    path "sample_sheet.csv"
  
  script:
    def outdir_param = outdir ? "--outdir ${outdir}" : ''
    def ignore_existing_param = ignore_existing ? "--ignore-existing" : ''
    """
    # Call the Python script (stored under scripts/) to generate the sample sheet.
    # This script lists raw FASTQ files and (if present) existing SIZ outputs, then writes sample_sheet.csv.
    python3 ${script} --bucket ${bucket} --delivery ${delivery} ${outdir_param} ${ignore_existing_param} --output sample_sheet.csv
    """
}
