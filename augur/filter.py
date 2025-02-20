"""
Filter and subsample a sequence set.
"""

from Bio import SeqIO
from collections import defaultdict
from typing import Collection
import random, os, re
import pandas as pd
import numpy as np
import sys
import datetime
from tempfile import NamedTemporaryFile
import treetime.utils

from .index import index_sequences
from .io import open_file, read_sequences, write_sequences
from .utils import read_metadata, read_strains, get_numerical_dates, run_shell_command, shquote, is_date_ambiguous

comment_char = '#'
MAX_NUMBER_OF_PROBABILISTIC_SAMPLING_ATTEMPTS = 10


def read_vcf(filename):
    if filename.lower().endswith(".gz"):
        import gzip
        file = gzip.open(filename, mode="rt", encoding='utf-8')
    else:
        file = open(filename, encoding='utf-8')

    chrom_line = next(line for line in file if line.startswith("#C"))
    file.close()
    headers = chrom_line.strip().split("\t")
    sequences = headers[headers.index("FORMAT") + 1:]

    # because we need 'seqs to remove' for VCF
    return sequences, sequences.copy()


def write_vcf(input_filename, output_filename, dropped_samps):
    if _filename_gz(input_filename):
        input_arg = "--gzvcf"
    else:
        input_arg = "--vcf"

    if _filename_gz(output_filename):
        output_pipe = "| gzip -c"
    else:
        output_pipe = ""

    drop_args = ["--remove-indv " + shquote(s) for s in dropped_samps]

    call = ["vcftools"] + drop_args + [input_arg, shquote(input_filename), "--recode --stdout", output_pipe, ">", shquote(output_filename)]

    print("Filtering samples using VCFTools with the call:")
    print(" ".join(call))
    run_shell_command(" ".join(call), raise_errors = True)
    # remove vcftools log file
    try:
        os.remove('out.log')
    except OSError:
        pass

def read_priority_scores(fname):
    try:
        with open(fname, encoding='utf-8') as pfile:
            return defaultdict(float, {
                elems[0]: float(elems[1])
                for elems in (line.strip().split('\t') if '\t' in line else line.strip().split() for line in pfile.readlines())
            })
    except Exception as e:
        print(f"ERROR: missing or malformed priority scores file {fname}", file=sys.stderr)
        raise e

def filter_by_query(sequences, metadata_file, query):
    """Filter a set of sequences using Pandas DataFrame querying against the metadata file.

    Parameters
    ----------
    sequences : list[str]
        List of sequence names to filter
    metadata_file : str
        Path to the metadata associated wtih the sequences
    query : str
        Query string for the dataframe.

    Returns
    -------
    list[str]:
        List of sequence names that match the given query
    """
    filtered_meta_dict, _ = read_metadata(metadata_file, query)
    return [seq for seq in sequences if seq in filtered_meta_dict]

def register_arguments(parser):
    input_group = parser.add_argument_group("inputs", "metadata and sequences to be filtered")
    input_group.add_argument('--metadata', required=True, metavar="FILE", help="sequence metadata, as CSV or TSV")
    input_group.add_argument('--sequences', '-s', help="sequences in FASTA or VCF format")
    input_group.add_argument('--sequence-index', help="sequence composition report generated by augur index. If not provided, an index will be created on the fly.")

    metadata_filter_group = parser.add_argument_group("metadata filters", "filters to apply to metadata")
    metadata_filter_group.add_argument(
        '--query',
        help="""Filter samples by attribute.
        Uses Pandas Dataframe querying, see https://pandas.pydata.org/pandas-docs/stable/user_guide/indexing.html#indexing-query for syntax.
        (e.g., --query "country == 'Colombia'" or --query "(country == 'USA' & (division == 'Washington'))")"""
    )
    metadata_filter_group.add_argument('--min-date', type=numeric_date, help="minimal cutoff for date; may be specified as an Augur-style numeric date (with the year as the integer part) or YYYY-MM-DD")
    metadata_filter_group.add_argument('--max-date', type=numeric_date, help="maximal cutoff for date; may be specified as an Augur-style numeric date (with the year as the integer part) or YYYY-MM-DD")
    metadata_filter_group.add_argument('--exclude-ambiguous-dates-by', choices=['any', 'day', 'month', 'year'],
                                help='Exclude ambiguous dates by day (e.g., 2020-09-XX), month (e.g., 2020-XX-XX), year (e.g., 200X-10-01), or any date fields. An ambiguous year makes the corresponding month and day ambiguous, too, even if those fields have unambiguous values (e.g., "201X-10-01"). Similarly, an ambiguous month makes the corresponding day ambiguous (e.g., "2010-XX-01").')
    metadata_filter_group.add_argument('--exclude', type=str, nargs="+", help="file(s) with list of strains to exclude")
    metadata_filter_group.add_argument('--exclude-where', nargs='+',
                                help="Exclude samples matching these conditions. Ex: \"host=rat\" or \"host!=rat\". Multiple values are processed as OR (matching any of those specified will be excluded), not AND")
    metadata_filter_group.add_argument('--exclude-all', action="store_true", help="exclude all strains by default. Use this with the include arguments to select a specific subset of strains.")
    metadata_filter_group.add_argument('--include', type=str, nargs="+", help="file(s) with list of strains to include regardless of priorities or subsampling")
    metadata_filter_group.add_argument('--include-where', nargs='+',
                                help="Include samples with these values. ex: host=rat. Multiple values are processed as OR (having any of those specified will be included), not AND. This rule is applied last and ensures any sequences matching these rules will be included.")

    sequence_filter_group = parser.add_argument_group("sequence filters", "filters to apply to sequence data")
    sequence_filter_group.add_argument('--min-length', type=int, help="minimal length of the sequences")
    sequence_filter_group.add_argument('--non-nucleotide', action='store_true', help="exclude sequences that contain illegal characters")

    subsample_group = parser.add_argument_group("subsampling", "options to subsample filtered data")
    subsample_group.add_argument('--group-by', nargs='+', help="categories with respect to subsample; two virtual fields, \"month\" and \"year\", are supported if they don't already exist as real fields but a \"date\" field does exist")
    subsample_limits_group = subsample_group.add_mutually_exclusive_group()
    subsample_limits_group.add_argument('--sequences-per-group', type=int, help="subsample to no more than this number of sequences per category")
    subsample_limits_group.add_argument('--subsample-max-sequences', type=int, help="subsample to no more than this number of sequences")
    probabilistic_sampling_group = subsample_group.add_mutually_exclusive_group()
    probabilistic_sampling_group.add_argument('--probabilistic-sampling', action='store_true', help="Enable probabilistic sampling during subsampling. This is useful when there are more groups than requested sequences. This option only applies when `--subsample-max-sequences` is provided.")
    probabilistic_sampling_group.add_argument('--no-probabilistic-sampling', action='store_false', dest='probabilistic_sampling')
    subsample_group.add_argument('--priority', type=str, help="""tab-delimited file with list of priority scores for strains (e.g., "<strain>\\t<priority>") and no header.
    When scores are provided, Augur converts scores to floating point values, sorts strains within each subsampling group from highest to lowest priority, and selects the top N strains per group where N is the calculated or requested number of strains per group.
    Higher numbers indicate higher priority.
    Since priorities represent relative values between strains, these values can be arbitrary.""")
    subsample_group.add_argument('--subsample-seed', help="random number generator seed to allow reproducible sub-sampling (with same input data). Can be number or string.")

    output_group = parser.add_argument_group("outputs", "possible representations of filtered data (at least one required)")
    output_group.add_argument('--output', '--output-sequences', '-o', help="filtered sequences in FASTA format")
    output_group.add_argument('--output-metadata', help="metadata for strains that passed filters")
    output_group.add_argument('--output-strains', help="list of strains that passed filters (no header)")

    parser.set_defaults(probabilistic_sampling=True)

def run(args):
    '''
    filter and subsample a set of sequences into an analysis set
    '''
    # Validate arguments before attempting any I/O.
    # Don't allow sequence output when no sequence input is provided.
    if args.output and not args.sequences:
        print(
            "ERROR: You need to provide sequences to output sequences.",
            file=sys.stderr)
        return 1

    # Confirm that at least one output was requested.
    if not any((args.output, args.output_metadata, args.output_strains)):
        print(
            "ERROR: You need to select at least one output.",
            file=sys.stderr)
        return 1

    # Don't allow filtering on sequence-based information, if no sequences or
    # sequence index is provided.
    SEQUENCE_ONLY_FILTERS = [
        args.min_length,
        args.non_nucleotide
    ]
    if not args.sequences and not args.sequence_index and any(SEQUENCE_ONLY_FILTERS):
        print(
            "ERROR: You need to provide a sequence index or sequences to filter on sequence-specific information.",
            file=sys.stderr)
        return 1

    # Load inputs, starting with metadata.
    try:
        # Metadata are the source of truth for which sequences we want to keep
        # in filtered output.
        meta_dict, meta_columns = read_metadata(args.metadata)
        metadata_strains = set(meta_dict.keys())
    except ValueError as error:
        print("ERROR: Problem reading in {}:".format(args.metadata))
        print(error)
        return 1

    #Set flags if VCF
    is_vcf = False
    is_compressed = False
    if args.sequences and any([args.sequences.lower().endswith(x) for x in ['.vcf', '.vcf.gz']]):
        is_vcf = True
        if args.sequences.lower().endswith('.gz'):
            is_compressed = True

    ### Check users has vcftools. If they don't, a one-blank-line file is created which
    #   allows next step to run but error very badly.
    if is_vcf:
        from shutil import which
        if which("vcftools") is None:
            print("ERROR: 'vcftools' is not installed! This is required for VCF data. "
                  "Please see the augur install instructions to install it.")
            return 1

    # Read in files

    # If VCF, open and get sequence names
    if is_vcf:
        vcf_sequences, _ = read_vcf(args.sequences)
        sequence_strains = set(vcf_sequences)
    elif args.sequences or args.sequence_index:
        # If FASTA, try to load the sequence composition details and strain
        # names to be filtered.
        index_is_autogenerated = False
        sequence_index_path = args.sequence_index

        # Generate the sequence index on the fly, for backwards compatibility
        # with older workflows that don't generate the index ahead of time.
        if sequence_index_path is None:
            # Create a temporary index using a random filename to avoid
            # collisions between multiple filter commands.
            index_is_autogenerated = True
            with NamedTemporaryFile(delete=False) as sequence_index_file:
                sequence_index_path = sequence_index_file.name

            print(
                f"WARNING: A sequence index was not provided, so we are generating one.",
                "Generate your own index ahead of time with `augur index` and pass it with `augur filter --sequence-index`.",
                file=sys.stderr
            )
            index_sequences(args.sequences, sequence_index_path)

        sequence_index = pd.read_csv(
            sequence_index_path,
            sep="\t"
        )

        # Remove temporary index file, if it exists.
        if index_is_autogenerated:
            os.unlink(sequence_index_path)

        # Calculate summary statistics needed for filtering.
        sequence_index["ACGT"] = sequence_index.loc[:, ["A", "C", "G", "T"]].sum(axis=1)
        sequence_strains = set(sequence_index["strain"].values)
    else:
        sequence_strains = None

    if sequence_strains is not None:
        # Calculate the number of strains that don't exist in either metadata or sequences.
        num_excluded_by_lack_of_metadata = len(sequence_strains - metadata_strains)
        num_excluded_by_lack_of_sequences = len(metadata_strains - sequence_strains)

        # Intersect sequence strain names with metadata strains.
        available_strains = metadata_strains & sequence_strains
    else:
        num_excluded_by_lack_of_metadata = None
        num_excluded_by_lack_of_sequences = None

        # When no sequence data are available, we treat the metadata as the
        # source of truth.
        available_strains = metadata_strains

    # Track the strains that are available to select by the filters below, after
    # accounting for availability of metadata and sequences.
    seq_keep = available_strains.copy()

    #####################################
    #Filtering steps
    #####################################

    # Exclude all strains by default.
    if args.exclude_all:
        num_excluded_by_all = len(available_strains)
        seq_keep = set()

    # remove strains explicitly excluded by name
    # read list of strains to exclude from file and prune seq_keep
    num_excluded_by_name = 0
    if args.exclude:
        try:
            to_exclude = read_strains(*args.exclude)
            num_excluded_by_name = len(seq_keep & to_exclude)
            seq_keep = seq_keep - to_exclude
        except FileNotFoundError as e:
            print("ERROR: Could not open file of excluded strains '%s'" % args.exclude, file=sys.stderr)
            sys.exit(1)

    # exclude strain my metadata field like 'host=camel'
    # match using lowercase
    num_excluded_by_metadata = {}
    if args.exclude_where:
        for ex in args.exclude_where:
            try:
                col, val = re.split(r'!?=', ex)
            except (ValueError,TypeError):
                print("invalid --exclude-where clause \"%s\", should be of from property=value or property!=value"%ex)
            else:
                to_exclude = set()
                for seq_name in seq_keep:
                    if "!=" in ex: # i.e. property!=value requested
                        if meta_dict[seq_name].get(col,'unknown').lower() != val.lower():
                            to_exclude.add(seq_name)
                    else: # i.e. property=value requested
                        if meta_dict[seq_name].get(col,'unknown').lower() == val.lower():
                            to_exclude.add(seq_name)

                num_excluded_by_metadata[ex] = len(seq_keep & to_exclude)
                seq_keep = seq_keep - to_exclude

    # exclude strains by metadata, using Pandas querying
    num_excluded_by_query = 0
    if args.query:
        filtered = set(filter_by_query(list(seq_keep), args.metadata, args.query))
        num_excluded_by_query = len(seq_keep - filtered)
        seq_keep = filtered

    # filter by sequence length
    num_excluded_by_length = 0
    if args.min_length:
        if is_vcf: #doesn't make sense for VCF, ignore.
            print("WARNING: Cannot use min_length for VCF files. Ignoring...")
        else:
            is_in_seq_keep = sequence_index["strain"].isin(seq_keep)
            is_gte_min_length = sequence_index["ACGT"] >= args.min_length

            seq_keep_by_length = set(
                sequence_index[
                    (is_in_seq_keep) & (is_gte_min_length)
                ]["strain"].tolist()
            )

            num_excluded_by_length = len(seq_keep) - len(seq_keep_by_length)
            seq_keep = seq_keep_by_length

    # filter by ambiguous dates
    num_excluded_by_ambiguous_date = 0
    if args.exclude_ambiguous_dates_by and 'date' in meta_columns:
        seq_keep_by_date = set()
        for seq_name in seq_keep:
            if not is_date_ambiguous(meta_dict[seq_name]['date'], args.exclude_ambiguous_dates_by):
                seq_keep_by_date.add(seq_name)

        num_excluded_by_ambiguous_date = len(seq_keep) - len(seq_keep_by_date)
        seq_keep = seq_keep_by_date

    # filter by date
    num_excluded_by_date = 0
    if (args.min_date or args.max_date) and 'date' in meta_columns:
        dates = get_numerical_dates(meta_dict, fmt="%Y-%m-%d")
        tmp = {s for s in seq_keep if dates[s] is not None}
        if args.min_date:
            tmp = {s for s in tmp if (np.isscalar(dates[s]) or all(dates[s])) and np.max(dates[s])>args.min_date}
        if args.max_date:
            tmp = {s for s in tmp if (np.isscalar(dates[s]) or all(dates[s])) and np.min(dates[s])<args.max_date}
        num_excluded_by_date = len(seq_keep) - len(tmp)
        seq_keep = tmp

    # exclude sequences with non-nucleotide characters
    num_excluded_by_nuc = 0
    if args.non_nucleotide:
        is_in_seq_keep = sequence_index["strain"].isin(seq_keep)
        no_invalid_nucleotides = sequence_index["invalid_nucleotides"] == 0
        seq_keep_by_valid_nucleotides = set(
            sequence_index[
                (is_in_seq_keep) & (no_invalid_nucleotides)
            ]["strain"].tolist()
        )

        num_excluded_by_nuc = len(seq_keep) - len(seq_keep_by_valid_nucleotides)
        seq_keep = seq_keep_by_valid_nucleotides

    # subsampling. This will sort sequences into groups by meta data fields
    # specified in --group-by and then take at most --sequences-per-group
    # from each group. Within each group, sequences are optionally sorted
    # by a priority score specified in a file --priority
    # Fix seed for the RNG if specified
    if args.subsample_seed:
        random.seed(args.subsample_seed)
    num_excluded_subsamp = 0
    if args.group_by and (args.sequences_per_group or args.subsample_max_sequences):
        spg = args.sequences_per_group
        seq_names_by_group = defaultdict(list)

        for seq_name in seq_keep:
            group = []
            m = meta_dict[seq_name]
            # collect group specifiers
            for c in args.group_by:
                if c in m:
                    group.append(m[c])
                elif c in ['month', 'year'] and 'date' in m:
                    try:
                        year = int(m["date"].split('-')[0])
                    except:
                        print("WARNING: no valid year, skipping",seq_name, m["date"])
                        continue
                    if c=='month':
                        try:
                            month = int(m["date"].split('-')[1])
                        except:
                            month = random.randint(1,12)
                        group.append((year, month))
                    else:
                        group.append(year)
                else:
                    group.append('unknown')
            seq_names_by_group[tuple(group)].append(seq_name)

        #If didnt find any categories specified, all seqs will be in 'unknown' - but don't sample this!
        if len(seq_names_by_group)==1 and ('unknown' in seq_names_by_group or ('unknown',) in seq_names_by_group):
            print("WARNING: The specified group-by categories (%s) were not found."%args.group_by,
                  "No sequences-per-group sampling will be done.")
            if any([x in args.group_by for x in ['year','month']]):
                print("Note that using 'year' or 'year month' requires a column called 'date'.")
            print("\n")
        else:
            # Check to see if some categories are missing to warn the user
            group_by = set(['date' if cat in ['year','month'] else cat
                            for cat in args.group_by])
            missing_cats = [cat for cat in group_by if cat not in meta_columns]
            if missing_cats:
                print("WARNING:")
                if any([cat != 'date' for cat in missing_cats]):
                    print("\tSome of the specified group-by categories couldn't be found: ",
                          ", ".join([str(cat) for cat in missing_cats if cat != 'date']))
                if any([cat == 'date' for cat in missing_cats]):
                    print("\tA 'date' column could not be found to group-by year or month.")
                print("\tFiltering by group may behave differently than expected!\n")

            if args.priority: # read priorities
                priorities = read_priority_scores(args.priority)

            if spg is None:
                # this is only possible if we have imposed a maximum number of samples
                # to produce.  we need binary search until we have the correct spg.
                try:
                    length_of_sequences_per_group = [
                        len(sequences_in_group)
                        for sequences_in_group in seq_names_by_group.values()
                    ]

                    if args.probabilistic_sampling:
                        spg = _calculate_fractional_sequences_per_group(
                            args.subsample_max_sequences,
                            length_of_sequences_per_group
                        )
                    else:
                        spg = _calculate_sequences_per_group(
                            args.subsample_max_sequences,
                            length_of_sequences_per_group
                        )
                except TooManyGroupsError as ex:
                    print(f"ERROR: {ex}", file=sys.stderr)
                    sys.exit(1)
                print("sampling at {} per group.".format(spg))

            if args.probabilistic_sampling:
                random_generator = np.random.default_rng()

            # subsample each groups, either by taking the spg highest priority strains or
            # sampling at random from the sequences in the group
            seq_subsample = set()
            subsampling_attempts = 0

            # Attempt to subsample with the given constraints for a fixed number
            # of times. For small values of maximum sequences, subsampling can
            # randomly select zero sequences to keep. When this happens, we can
            # usually find a non-zero number of samples by repeating the
            # process.
            while len(seq_subsample) == 0 and subsampling_attempts < MAX_NUMBER_OF_PROBABILISTIC_SAMPLING_ATTEMPTS:
                subsampling_attempts += 1

                for group, sequences_in_group in seq_names_by_group.items():
                    if args.probabilistic_sampling:
                        tmp_spg = random_generator.poisson(spg)
                    else:
                        tmp_spg = spg

                    if tmp_spg == 0:
                        continue

                    if args.priority: #sort descending by priority
                        seq_subsample.update(
                            set(
                                sorted(
                                    sequences_in_group,
                                    key=lambda x: priorities[x],
                                    reverse=True
                                )[:tmp_spg]
                            )
                        )
                    else:
                        seq_subsample.update(
                            set(
                                sequences_in_group
                                if len(sequences_in_group)<=tmp_spg
                                else random.sample(sequences_in_group, tmp_spg)
                            )
                        )

            num_excluded_subsamp = len(seq_keep) - len(seq_subsample)
            seq_keep = seq_subsample

    # force include sequences specified in file.
    # Note that this might re-add previously excluded sequences
    # Note that we are also not checking for existing meta data here
    num_included_by_name = 0
    if args.include:
        # Collect the union of all given strains to include.
        to_include = read_strains(*args.include)

        # Find requested strains that can be included because they have metadata
        # and sequences.
        available_to_include = available_strains & to_include

        # Track the number of strains that could and could not be included.
        num_included_by_name = len(available_to_include)
        num_not_included_by_name = len(to_include - available_to_include)

        # Union the strains that can be included with the sequences to keep.
        seq_keep = seq_keep | available_to_include

    # add sequences with particular meta data attributes
    num_included_by_metadata = 0
    if args.include_where:
        to_include = set()

        for ex in args.include_where:
            try:
                col, val = ex.split("=")
            except (ValueError,TypeError):
                print("invalid include clause %s, should be of from property=value"%ex)
                continue

            # loop over all sequences and re-add sequences
            for seq_name in available_strains:
                if meta_dict[seq_name].get(col)==val:
                    to_include.add(seq_name)

        num_included_by_metadata = len(to_include)
        seq_keep = seq_keep | to_include

    # Write output starting with sequences, if they've been requested. It is
    # possible for the input sequences and sequence index to be out of sync
    # (e.g., the index is a superset of the given sequences input), so we need
    # to update the set of strains to keep based on which strains are actually
    # available.
    if is_vcf and args.output:
        # Get the samples to be deleted, not to keep, for VCF
        dropped_samps = list(available_strains - seq_keep)
        write_vcf(args.sequences, args.output, dropped_samps)
    elif args.sequences and args.output:
        sequences = read_sequences(args.sequences)

        # Stream to disk all sequences that passed all filters to avoid reading
        # sequences into memory first. Track the observed strain names in the
        # sequence file as part of the single pass to allow comparison with the
        # provided sequence index.
        observed_sequence_strains = set()
        with open_file(args.output, "wt") as output_handle:
            for sequence in sequences:
                observed_sequence_strains.add(sequence.id)

                if sequence.id in seq_keep:
                    write_sequences(sequence, output_handle, 'fasta')

        if sequence_strains != observed_sequence_strains:
            # Warn the user if the expected strains from the sequence index are
            # not a superset of the observed strains.
            if not observed_sequence_strains <= sequence_strains:
                print(
                    "WARNING: The sequence index is out of sync with the provided sequences.",
                    "Augur will only output strains with available sequences.",
                    file=sys.stderr
                )

            # Update the set of available sequence strains and which of these
            # strains passed filters. This prevents writing out strain lists or
            # metadata for strains that have no sequences.
            sequence_strains = observed_sequence_strains
            seq_keep = seq_keep & sequence_strains

            # Calculate the number of strains that don't exist in either
            # metadata or sequences.
            num_excluded_by_lack_of_metadata = len(sequence_strains - metadata_strains)
            num_excluded_by_lack_of_sequences = len(metadata_strains - sequence_strains)

    if args.output_metadata:
        metadata_df = pd.DataFrame([meta_dict[strain] for strain in seq_keep])
        metadata_df.to_csv(
            args.output_metadata,
            sep="\t",
            index=False
        )

    if args.output_strains:
        with open(args.output_strains, "w") as oh:
            for strain in sorted(seq_keep):
                oh.write(f"{strain}\n")

    # Calculate the number of strains passed and filtered.
    if sequence_strains is not None:
        all_strains = metadata_strains | sequence_strains
    else:
        all_strains = metadata_strains

    total_strains_passed = len(seq_keep)
    total_strains_filtered = len(all_strains) - total_strains_passed

    print(f"{total_strains_filtered} strains were dropped during filtering")

    if num_excluded_by_lack_of_sequences:
        print(f"\t{num_excluded_by_lack_of_sequences} had no sequence data")

    if num_excluded_by_lack_of_metadata:
        print(f"\t{num_excluded_by_lack_of_metadata} had no metadata")

    if args.exclude_all:
        print(f"\t{num_excluded_by_all} of these were dropped by `--exclude-all`")
    if args.exclude:
        print("\t%i of these were dropped because they were in %s" % (num_excluded_by_name, args.exclude))
    if args.exclude_where:
        for key,val in num_excluded_by_metadata.items():
            print("\t%i of these were dropped because of '%s'" % (val, key))
    if args.query:
        print("\t%i of these were filtered out by the query:\n\t\t\"%s\"" % (num_excluded_by_query, args.query))
    if args.min_length:
        print("\t%i of these were dropped because they were shorter than minimum length of %sbp" % (num_excluded_by_length, args.min_length))
    if args.exclude_ambiguous_dates_by and num_excluded_by_ambiguous_date:
        print("\t%i of these were dropped because of their ambiguous date in %s" % (num_excluded_by_ambiguous_date, args.exclude_ambiguous_dates_by))
    if (args.min_date or args.max_date) and 'date' in meta_columns:
        print("\t%i of these were dropped because of their date (or lack of date)" % (num_excluded_by_date))
    if args.non_nucleotide:
        print("\t%i of these were dropped because they had non-nucleotide characters" % (num_excluded_by_nuc))
    if args.group_by and args.sequences_per_group:
        seed_txt = ", using seed {}".format(args.subsample_seed) if args.subsample_seed else ""
        print("\t%i of these were dropped because of subsampling criteria%s" % (num_excluded_subsamp, seed_txt))

    if args.include:
        print(f"\n\t{num_included_by_name} strains were added back because they were requested by include files")

        if num_not_included_by_name:
            print(f"\t{num_not_included_by_name} strains from include files were not added because they lacked sequence or metadata")
    if args.include_where:
        print("\t%i sequences were added back because of '%s'" % (num_included_by_metadata, args.include_where))

    if total_strains_passed == 0:
        print("ERROR: All samples have been dropped! Check filter rules and metadata file format.", file=sys.stderr)
        return 1

    print(f"{total_strains_passed} strains passed all filters")


def _filename_gz(filename):
    return filename.lower().endswith(".gz")


def numeric_date(date):
    """
    Converts the given *date* string to a :py:class:`float`.

    *date* may be given as a number (a float) with year as the integer part, or
    in the YYYY-MM-DD (ISO 8601) syntax.

    >>> numeric_date("2020.42")
    2020.42
    >>> numeric_date("2020-06-04")
    2020.42486...
    """
    try:
        return float(date)
    except ValueError:
        return treetime.utils.numeric_date(datetime.date(*map(int, date.split("-", 2))))


class TooManyGroupsError(ValueError):
    def __init__(self, msg):
        self.msg = msg

    def __str__(self):
        return str(self.msg)


def _calculate_total_sequences(
        hypothetical_spg: float, sequence_lengths: Collection[int],
) -> float:
    # calculate how many sequences we'd keep given a hypothetical spg.
    return sum(
        min(hypothetical_spg, sequence_length)
        for sequence_length in sequence_lengths
    )


def _calculate_sequences_per_group(
        target_max_value: int,
        sequence_lengths: Collection[int]
) -> int:
    """This is partially inspired by
    https://github.com/python/cpython/blob/3.8/Lib/bisect.py

    This should return the spg such that we don't exceed the requested
    number of samples.

    Parameters
    ----------
    target_max_value : int
        the total number of sequences allowed across all groups
    sequence_lengths : Collection[int]
        the number of sequences in each group

    Returns
    -------
    int
        maximum number of sequences allowed per group to meet the required maximum total
        sequences allowed

    >>> _calculate_sequences_per_group(4, [4, 2])
    2
    >>> _calculate_sequences_per_group(2, [4, 2])
    1
    >>> _calculate_sequences_per_group(1, [4, 2])
    Traceback (most recent call last):
        ...
    augur.filter.TooManyGroupsError: Asked to provide at most 1 sequences, but there are 2 groups.
    """

    if len(sequence_lengths) > target_max_value:
        # we have more groups than sequences we are allowed, which is an
        # error.

        raise TooManyGroupsError(
            "Asked to provide at most {} sequences, but there are {} "
            "groups.".format(target_max_value, len(sequence_lengths)))

    lo = 1
    hi = target_max_value

    while hi - lo > 2:
        mid = (hi + lo) // 2
        if _calculate_total_sequences(mid, sequence_lengths) <= target_max_value:
            lo = mid
        else:
            hi = mid

    if _calculate_total_sequences(hi, sequence_lengths) <= target_max_value:
        return int(hi)
    else:
        return int(lo)


def _calculate_fractional_sequences_per_group(
        target_max_value: int,
        sequence_lengths: Collection[int]
) -> float:
    """Returns the fractional sequences per group for the given list of group
    sequences such that the total doesn't exceed the requested number of
    samples.

    Parameters
    ----------
    target_max_value : int
        the total number of sequences allowed across all groups
    sequence_lengths : Collection[int]
        the number of sequences in each group

    Returns
    -------
    float
        fractional maximum number of sequences allowed per group to meet the
        required maximum total sequences allowed

    >>> np.around(_calculate_fractional_sequences_per_group(4, [4, 2]), 4)
    1.9375
    >>> np.around(_calculate_fractional_sequences_per_group(2, [4, 2]), 4)
    0.9688

    Unlike the integer-based version of this function, the fractional version
    can accept a maximum number of sequences that exceeds the number of groups.
    In this case, the function returns a fraction that can be used downstream,
    for example with Poisson sampling.

    >>> np.around(_calculate_fractional_sequences_per_group(1, [4, 2]), 4)
    0.4844
    """
    lo = 1e-5
    hi = target_max_value

    while (hi / lo) > 1.1:
        mid = (lo + hi) / 2
        if _calculate_total_sequences(mid, sequence_lengths) <= target_max_value:
            lo = mid
        else:
            hi = mid

    return (lo + hi) / 2
