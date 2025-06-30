#!/usr/bin/env nextflow

include { SIZER } from './modules/local/sizer.nf'
include { GENERATE_SAMPLESHEET } from './modules/local/gen_samplesheet.nf'

workflow {

  // Determine the sample sheet channel
  def sampleSheetChannel

  //  If --sample_sheet is provided, use it.
  if ( params.sample_sheet ) {
    println "Using provided sample sheet: ${params.sample_sheet}"
    sampleSheetChannel = Channel.fromPath(params.sample_sheet)
  } 
  // Otherwise, generate it from bucket and delivery parameters.
  else if ( params.bucket && params.delivery ) {
    println "No sample sheet provided; generating sample sheet from bucket ${params.bucket} and delivery ${params.delivery}"
    def script = file("scripts/generate_samplesheet.py")
    sampleSheetChannel = GENERATE_SAMPLESHEET(
      params.bucket, 
      params.delivery,
      params.outdir ? params.outdir : '',
      params.ignore_existing,
      script
    )
  } else {
    error "You must provide either --sample_sheet or both --bucket and --delivery"
  }

  // Create a channel from the sample sheet CSV.
  ids_ch = sampleSheetChannel
    .splitCsv(header:true)
    .map { row ->
      def sizer_params = [
        chunk_size: params.read_pairs_per_siz,
        zstd_level: params.zstd_level
      ]
      tuple(row.id, row.fastq_1, row.fastq_2, row.outdir, sizer_params)
    }
  
  // spawn a process for each pair of files to SIZer
  SIZER(ids_ch)
}
