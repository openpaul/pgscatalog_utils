import argparse
import textwrap
import logging
import os

import pandas as pd

import pgscatalog_utils.config as config
from pgscatalog_utils.ancestry.read import read_pcs, read_pgs, extract_ref_psam_cols
from pgscatalog_utils.ancestry.tools import assign_ancestry, assign_method_threshold, choose_pval_threshold, \
    pgs_adjust, normalization_methods

logger = logging.getLogger(__name__)


def ancestry_analysis():
    args = _parse_args()
    config.set_logging_level(args.verbose)
    config.OUTDIR = args.outdir

    # Load PCA data
    maxPCs = max([10, args.nPCs_assignment, args.nPCs_normalization])  # save memory by not using all PCs
    loc_ref_pcs = args.ref_pcs
    reference_df = read_pcs(loc_pcs=loc_ref_pcs, dataset=args.d_ref,
                            loc_related_ids=args.ref_related, nPCs=maxPCs)
    loc_ref_psam = args.psam
    reference_df = extract_ref_psam_cols(loc_ref_psam, args.d_ref, reference_df, keepcols=[args.ref_label])

    loc_target_sscores = args.target_pcs
    target_df = read_pcs(loc_pcs=loc_target_sscores, dataset=args.d_target, nPCs=maxPCs)

    # Load PGS data & merge with PCA data
    pgs = read_pgs(args.scorefile, onlySUM=True)
    scorecols = list(pgs.columns)
    reference_df = pd.merge(reference_df, pgs, left_index=True, right_index=True)
    target_df = pd.merge(target_df, pgs, left_index=True, right_index=True)
    del pgs  # clear raw PGS from memory


    # Assign ancestry
    assignment_threshold_p = choose_pval_threshold(args)
    ancestry_ref, ancestry_target = assign_ancestry(ref_df=reference_df,
                                                    ref_pop_col=args.ref_label, ref_train_col='Unrelated',
                                                    target_df=target_df,
                                                    n_pcs=args.nPCs_assignment,
                                                    method=args.method_assignment,
                                                    p_threshold=assignment_threshold_p)

    reference_df = pd.concat([reference_df, ancestry_ref], axis=1)
    target_df = pd.concat([target_df, ancestry_target], axis=1)
    del ancestry_ref, ancestry_target

    # Adjust PGS values
    adjpgs_ref, adjpgs_target = pgs_adjust(reference_df, target_df, scorecols,
                                           args.ref_label, 'PopAssignment',
                                           use_method=args.method_normalization,
                                           ref_train_col='Unrelated',
                                           n_pcs=args.nPCs_normalization)
    adjpgs = pd.concat([adjpgs_ref, adjpgs_target], axis=0)
    del adjpgs_ref, adjpgs_target

    # Write outputs
    dout = os.path.abspath(config.OUTDIR)
    reference_df['REFERENCE'] = True
    target_df['REFERENCE'] = False
    final_df = pd.concat([target_df, reference_df], axis=0)
    del reference_df, target_df

    # Melt PGS
    adjpgs = adjpgs.melt(ignore_index=False)
    adjpgs[['method', 'PGS']] = adjpgs.variable.str.split("|", expand=True)
    # Currently each PGS will have it's own row... but it might be more optimal for each normalization method
    #  to be on separate rows? My logic is that you might want to check correaltion between methods and it is easiest
    #  in this format.
    adjpgs = adjpgs.drop('variable', axis=1).reset_index().pivot(index=['sampleset', 'IID', 'PGS'], columns='method', values='value')
    adjpgs.to_csv(os.path.join(dout, f"{args.d_target}_pgs.txt.gz"), sep='\t')

    # Write ancestry
    final_df.drop(scorecols, axis=1).to_csv(os.path.join(dout, f"{args.d_target}_ancestry.txt.gz"), sep='\t')


def _description_text() -> str:
    return textwrap.dedent('Program to analyze ancestry outputs of the pgscatalog/pgsc_calc pipeline. Current inputs: '
                           '\n  - PCA projections from reference and target datasets (*.pcs)'
                           '\n  - calculated polygenic scores (e.g. aggregated_scores.txt.gz), '
                           '\n  - information about related samples in the reference dataset (e.g. '
                           'deg2_hg38.king.cutoff.out.id).')


def _parse_args(args=None):
    parser = argparse.ArgumentParser(description=_description_text(),
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('-d', '--dataset', dest='d_target', required=True,
                        help='<Required> Label of the TARGET genomic dataset')
    parser.add_argument('-r', '--reference', dest='d_ref', required=True,
                        help='<Required> Label of the REFERENCE genomic dataset')
    parser.add_argument('--ref_pcs', dest='ref_pcs', required=True, nargs='+',
                        help='<Required> Principal components path (output from fraposa_pgsc)')
    parser.add_argument('--target_pcs', dest='target_pcs', required=True, nargs='+',
                        help='<Required> Principal components path (output from fraposa_pgsc)')
    parser.add_argument('--psam', dest='psam', required=True,
                        help='<Required> Reference sample information file path in plink2 psam format)')
    parser.add_argument('-x', '--reference_related', dest='ref_related',
                        help='File of related sample IDs (excluded from training ancestry assignments)')
    parser.add_argument('-p', '--pop_label', dest='ref_label', default='SuperPop',
                        help='Population labels in REFERENCE psam to use for assignment')
    parser.add_argument('-s', '--agg_scores', dest='scorefile', default='aggregated_scores.txt.gz',
                        help='Aggregated scores in PGS Catalog format ([sampleset, IID] indexed)')
    parser.add_argument('-a', '--assignment_method', dest='method_assignment',
                        choices=assign_method_threshold.keys(), default='RandomForest',
                        help='Method used for population/ancestry assignment')
    parser.add_argument('--n_assignment', dest='nPCs_assignment', type=int, metavar="[1-20]", choices=range(1, 21),
                        default=10,
                        help='Number of PCs used for population ASSIGNMENT (default = 10)')
    parser.add_argument('-t', '--pval_threshold', dest='pThreshold', type=float,
                        help='p-value threshold used to exclude low-confidence ancestry assignments')
    parser.add_argument('-n', '--normalization_method', nargs='+', dest='method_normalization',
                        choices=normalization_methods, default=["empirical", "mean", "mean+var"],
                        help='Method used for normalization of genetic ancestry')
    parser.add_argument('--n_normalization', dest='nPCs_normalization', type=int, metavar="[1-20]",
                        choices=range(1, 21), default=5,
                        help='Number of PCs used for population NORMALIZATION (default = 5)')
    parser.add_argument('--outdir', dest='outdir', required=True,
                        help='<Required> Output directory')
    parser.add_argument('-v', '--verbose', dest='verbose', action='store_true',
                        help='<Optional> Extra logging information')
    return parser.parse_args(args)


if __name__ == "__main__":
    ancestry_analysis()
