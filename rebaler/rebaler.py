# -*- coding: utf-8 -*-
"""
Copyright 2017 Ryan Wick (rrwick@gmail.com)
https://github.com/rrwick/Rebaler

This file is part of Rebaler. Rebaler is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by the Free Software Foundation,
either version 3 of the License, or (at your option) any later version. Rebaler is distributed in
the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
details. You should have received a copy of the GNU General Public License along with Rebaler. If
not, see <http://www.gnu.org/licenses/>.
"""

    
import argparse
import multiprocessing
import os
import sys
import subprocess
import itertools
import collections
import shutil

from .misc import MyHelpFormatter, load_fasta, load_fasta_or_fastq, int_to_str, \
    print_table, red, colour, get_right_arrow
from .alignment import Alignment
from . import log
from .unitig_graph import UnitigGraph


__version__ = "0.1.1"


def main():
    args = get_arguments()
    log.logger = log.Log()

    log.log_section_header('Loading reference')
    log.log_explanation('This reference sequence will be used as a template for the Rebaler '
                        'assembly.')
    reference = load_fasta(args.reference)
    ref_names = [x[0] for x in reference]
    circularity = {x[0]: 'circular=true' in x[2].lower() for x in reference}
    ref_seqs = {x[0]: x[1] for x in reference}
    print_ref_info(ref_names, ref_seqs, circularity)

    log.log_section_header('Building unpolished assembly')
    log.log_explanation('Rebaler first aligns long reads to the reference using minimap2. It then '
                        'selects high quality alignments and replaces the reference sequence with '
                        'the corresponding read sequence. This creates an unpolished assembly '
                        'made directly from read fragments, similar to what would be produced by '
                        'miniasm.')
    log.log('Loading reads...                             ', end='')
    reads, _ = load_fasta_or_fastq(args.reads)
    log.log(int_to_str(len(reads)) + ' reads')
    nicknames = get_read_nickname_dict([x[0] for x in reads])

    log.log('Aligning reads to reference with minimap2... ', end='')
    alignments = get_initial_alignments(args)
    log.log(int_to_str(len(alignments)) + ' initial alignments')

    log.log('Culling alignments to a non-redundant set... ', end='')
    alignments, depths = cull_alignments(alignments, reference)
    for depth_list in depths.values():
        assert all(0 <= x <= 2 for x in depth_list)
    log.log(int_to_str(len(alignments)) + ' alignments remain')

    log.log('\nConstructing unpolished assembly:')
    store_read_seqs_in_alignments(alignments, reads)
    partitions = partition_reference(reference, alignments)
    print_partitions(ref_names, partitions, nicknames, ref_seqs)
    unpolished_sequences = get_unpolished_sequences(partitions, ref_seqs)

    log.log_section_header('Polishing assembly')
    log.log_explanation('Rebaler now runs Racon to polish the miniasm assembly. It does '
                        'multiple rounds of polishing to get the best consensus. Circular unitigs '
                        'are rotated between rounds such that all parts (including the ends) are '
                        'polished well. Assembly quality is measured by the sum of all read '
                        'alignment scores.')
    polish_dir = 'temp_rebaler_' + str(os.getpid())
    unitig_graph = polish_assembly_with_racon(ref_names, unpolished_sequences, circularity,
                                              args.reads, args.threads, polish_dir)
    unitig_graph.print_fasta_to_stdout(ref_names)

    if not args.keep:
        log.log('\nDeleting temp directory ' + polish_dir)
        shutil.rmtree(polish_dir, ignore_errors=True)
    log.log('')


def get_arguments():
    """
    Parse the command line arguments.
    """
    default_threads = min(multiprocessing.cpu_count(), 16)

    parser = argparse.ArgumentParser(description='Rebaler: reference-based long read assemblies '
                                                 'of bacterial genomes',
                                     formatter_class=MyHelpFormatter)
    parser.add_argument('-t', '--threads', type=int, default=default_threads,
                        help='Number of threads to use for alignment and polishing')
    parser.add_argument('--keep', action='store_true',
                        help='Do not delete temp directory of intermediate files (default: '
                             'delete temp directory)')
    parser.add_argument('reference', type=str,
                        help='FASTA file of reference assembly')
    parser.add_argument('reads', type=str,
                        help='FASTA/FASTQ file of long reads')

    if len(sys.argv) == 1:
        parser.print_help(file=sys.stderr)
        sys.exit(1)

    args = parser.parse_args()
    if not os.path.isfile(args.reference):
        sys.exit('Error: could not find ' + args.reference)
    if not os.path.isfile(args.reads):
        sys.exit('Error: could not find ' + args.reference)

    return args


def print_ref_info(names, seqs, circularity):
    table = [['Reference contig', 'Circular', 'Length']]
    for name in names:
        table.append([name, 'yes' if circularity[name] else 'no', int_to_str(len(seqs[name]))])
    print_table(table, left_align_header=False, alignments='LLR', indent=0)


def get_initial_alignments(args):
    alignments = []
    command = ['minimap2', '-c', '-x', 'map-ont', '-t', str(args.threads),
               args.reference, args.reads]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    while process.poll() is None:
        paf_line = process.stdout.readline().rstrip().decode()
        if paf_line:
            alignments.append(Alignment(paf_line))
    return alignments


def cull_alignments(alignments, reference):
    """
    Takes the alignments and removes redundant ones, preferentially keeping higher quality reads.
    """
    depths = get_reference_depths(alignments, reference)

    alignments = sorted(alignments, reverse=True, key=lambda x: x.quality)
    alignment_count = len(alignments)

    for i in range(alignment_count-1, -1, -1):
        a = alignments[i]
        name = a.ref_name
        can_delete = all(x > 1 for x in depths[name][a.ref_start:a.ref_end])
        if can_delete:
            alignments.pop(i)
            for j in range(a.ref_start, a.ref_end):
                depths[name][j] -= 1

    return alignments, depths


def get_reference_depths(alignments, reference):
    """
    Returns per-base depth of coverage for the reference contigs.
    """
    depths = {}
    for name, seq, _ in reference:
        depths[name] = [0] * len(seq)
    for a in alignments:
        name = a.ref_name
        for i in range(a.ref_start, a.ref_end):
            depths[name][i] += 1
    return depths


def store_read_seqs_in_alignments(alignments, reads):
    read_seqs = {}
    for r in reads:
        read_seqs[r[0]] = r[1]
    for a in alignments:
        a.add_read_sequence(read_seqs[a.read_name])


def partition_reference(reference, alignments):
    """
    This function splits the reference up into chunks based on the alignments. Each chunk has the
    start and end positions (in reference coordinates) and the associated alignment.
    """
    partitions = {}
    for name, seq, _ in reference:
        partitions[name] = []
        ref_len = len(seq)
        ref_alignments = [x for x in alignments if x.ref_name == name]
        ref_alignments = sorted(ref_alignments, key=lambda x: x.ref_start)

        for i in range(len(ref_alignments)):
            a = ref_alignments[i]
            if i > 0:
                a_prev = ref_alignments[i-1]
                prev_overlap = a_prev.ref_end > a.ref_start
            else:
                a_prev = None
                prev_overlap = False
            try:
                a_next = ref_alignments[i+1]
                next_overlap = a.ref_end > a_next.ref_start
            except IndexError:
                a_next = None
                next_overlap = False

            # Make the starting gap, if appropriate.
            if a_prev is None and a.ref_start > 0:
                partitions[name].append((0, a.ref_start, None))
            if a_prev is not None and a_prev.ref_end < a.ref_start:
                partitions[name].append((a_prev.ref_end, a.ref_start, None))

            # Make the depth=1 partition.
            if prev_overlap:
                start = a_prev.ref_end
            else:
                start = a.ref_start
            if next_overlap:
                end = a_next.ref_start
            else:
                end = a.ref_end
            partitions[name].append((start, end, a))

            # Make the depth=2 partition.
            if next_overlap:
                best_a = a if a.percent_identity > a_next.percent_identity else a_next
                partitions[name].append((a_next.ref_start, a.ref_end, best_a))

            # Make the ending gap, if appropriate.
            if a_next is None and a.ref_end < ref_len:
                partitions[name].append((a.ref_end, ref_len, None))

    return partitions


def print_partitions(names, partitions, nicknames, ref_seqs):
    arrow = ' ' + get_right_arrow() + ' '
    for name in names:
        contig_partitions = partitions[name]
        output_parts = []
        log.log('\n' + name + ':')
        for start, end, alignment in contig_partitions:

            if alignment is None:
                read_name = 'reference(+)'
                read_start, read_end = start, end
            else:
                read_name = nicknames[alignment.read_name] + '(' + alignment.read_strand + ')'
                _, read_start, read_end = alignment.get_read_seq_by_ref_coords(start, end,
                                                                               ref_seqs[name])
            # Merge this output part in with the previous, if applicable.
            if len(output_parts) > 0:
                prev_read_name, prev_start, prev_end = output_parts[-1]
            else:
                prev_read_name, prev_start, prev_end = '', 0, 0
            if read_name == prev_read_name and read_start == prev_end:
                output_parts.pop()
                output_parts.append((read_name, prev_start, read_end))
            else:
                output_parts.append((read_name, read_start, read_end))

        output_parts_str = []
        for read_name, read_start, read_end in output_parts:
            range_str = ':' + str(read_start) + '-' + str(read_end)
            str_colour = 'red' if read_name == 'reference(+)' else 'green'
            output_parts_str.append(colour(read_name + range_str, str_colour))
        log.log(arrow.join(output_parts_str))


def get_unpolished_sequences(partitions, ref_seqs):
    """
    This function goes through the partitions and returns
    """
    unpolished_sequences = {}
    for name, ref_partitions in partitions.items():
        seq_parts = []
        ref_seq = ref_seqs[name]
        for start, end, alignment in ref_partitions:

            # If there is no alignment, then the reference sequence is used for this part.
            if alignment is None:
                seq_parts.append(ref_seq[start:end])

            # If there is an alignment, then the sequence is taken from the read.
            else:
                seq_parts.append(alignment.get_read_seq_by_ref_coords(start, end, ref_seq)[0])

        seq = ''.join(seq_parts)
        unpolished_sequences[name] = seq
    return unpolished_sequences


def polish_assembly_with_racon(names, unpolished_sequences, circularity, polish_reads, threads,
                               polish_dir):
    if not os.path.isdir(polish_dir):
        os.makedirs(polish_dir)

    unitig_graph = UnitigGraph(names, unpolished_sequences, circularity)

    col_widths = [6, 12, 14]
    racon_table_header = ['Polish round', 'Assembly size', 'Mapping quality']
    print_table([racon_table_header], fixed_col_widths=col_widths, left_align_header=False,
                alignments='LRR', indent=0)

    best_fasta = None
    best_unitig_sequences = {}
    best_mapping_quality = 0
    times_quality_failed_to_beat_best = 0

    counter = itertools.count(start=1)
    round_num = '%02d' % next(counter)
    current_fasta = os.path.join(polish_dir, round_num + '_unpolished_assembly.fasta')
    current_gfa = os.path.join(polish_dir, round_num + '_unpolished_assembly.gfa')
    unitig_graph.save_to_fasta(current_fasta)
    unitig_graph.save_to_gfa(current_gfa)

    racon_loop_count = 10
    for polish_round_count in range(racon_loop_count):

        # Prepare filenames
        round_num = '%02d' % next(counter)
        mappings_filename = os.path.join(polish_dir, round_num + '_1_alignments.paf')
        racon_log = os.path.join(polish_dir, round_num + '_2_racon.log')
        polished_fasta = os.path.join(polish_dir, round_num + '_3_polished.fasta')
        fixed_fasta = os.path.join(polish_dir, round_num + '_4_fixed.fasta')
        rotated_fasta = os.path.join(polish_dir, round_num + '_5_rotated.fasta')
        rotated_gfa = os.path.join(polish_dir, round_num + '_5_rotated.gfa')

        mapping_quality, unitig_depths = \
            make_racon_polish_alignments(current_fasta, mappings_filename, polish_reads, threads)
        for unitig_name, unitig_seg in unitig_graph.segments.items():
            if unitig_name in unitig_depths:
                unitig_seg.depth = unitig_depths[unitig_name]

        racon_table_row = ['begin' if polish_round_count == 0 else str(polish_round_count),
                           int_to_str(unitig_graph.get_total_segment_length()),
                           int_to_str(mapping_quality)]
        print_table([racon_table_row], fixed_col_widths=col_widths, left_align_header=False,
                    alignments='LRR', indent=0, header_format='normal', bottom_align_header=False)

        # Do we have a new best?
        if mapping_quality > best_mapping_quality:
            best_mapping_quality = mapping_quality
            best_fasta = current_fasta
            best_unitig_sequences = {name: seg.forward_sequence
                                     for name, seg in unitig_graph.segments.items()}
            times_quality_failed_to_beat_best = 0
        else:
            times_quality_failed_to_beat_best += 1

        # If we've failed to improve on our best quality for a few rounds, then we're done!
        if times_quality_failed_to_beat_best > 2:
            break

        # Run Racon. It crashes sometimes, so repeat until its return code is 0.
        command = ['racon', '-t', str(threads), polish_reads, mappings_filename, current_fasta]
        return_code = 1
        for _ in range(100):  # Only try a fixed number of times, to prevent an infinite loop.
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            out, err = process.communicate()
            with open(racon_log, 'wb') as log_file:
                log_file.write(err)
            with open(polished_fasta, 'wb') as racon_out:
                racon_out.write(out)
            return_code = process.returncode
            if return_code == 0 and os.path.isfile(polished_fasta):
                break
            if os.path.isfile(polished_fasta):
                os.remove(polished_fasta)
            if os.path.isfile(racon_log):
                os.remove(racon_log)

        # If even after all those tries Racon still didn't succeed, then we give up!
        if return_code != 0 or not os.path.isfile(polished_fasta):
            break

        unitig_graph.replace_with_polished_sequences(polished_fasta)
        unitig_graph.save_to_fasta(fixed_fasta)
        unitig_graph.rotate_circular_sequences()
        unitig_graph.save_to_fasta(rotated_fasta)
        unitig_graph.save_to_gfa(rotated_gfa)
        current_fasta = rotated_fasta

    log.log('')
    if best_fasta:
        log.log('Best polish: ' + best_fasta)
        for unitig_name, unitig_seq in best_unitig_sequences.items():
            segment = unitig_graph.segments[unitig_name]
            segment.forward_sequence = unitig_seq
        unitig_graph.normalise_read_depths()
    else:
        log.log(red('Polishing failed'))
    return unitig_graph


def make_racon_polish_alignments(current_fasta, mappings_filename, polish_reads, threads):
    mapping_quality = 0
    unitig_depths = collections.defaultdict(float)

    with open(mappings_filename, 'wt') as mappings:
        command = ['minimap2', '-c', '-x', 'map-ont', '-t', str(threads),
                   current_fasta, polish_reads]
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        while process.poll() is None:
            paf_line = process.stdout.readline().rstrip().decode()
            if paf_line:
                a = Alignment(paf_line)
                mapping_quality += a.alignment_score
                mappings.write(paf_line.split('cg:Z:')[0].rstrip())
                mappings.write('\n')
                unitig_depths[a.ref_name] += a.fraction_ref_aligned()
    return mapping_quality, unitig_depths


def get_read_nickname_dict(read_names):
    """
    Read names can be quite long, so for the sake of output brevity, this function tries to come
    up with some shorter nicknames for the reads.
    """
    nickname_dict = {}

    # Handle Albacore reads: if splitting on the first dash results in mostly unique values.
    before_dash = [n.split('-')[0] for n in read_names]
    if all(len(n) == 8 for n in before_dash):
        counter = collections.defaultdict(int)
        for n in before_dash:
            counter[n] += 1
        for n in read_names:
            nickname = n.split('-')[0]
            if counter[nickname] == 1:
                nickname_dict[n] = nickname
            else:
                nickname_dict[n] = n
        return nickname_dict

    # Find any common prefix.
    prefix = len(os.path.commonprefix(read_names))

    # Handle fast5 filename reads: if _ch and _read are in each read name.
    if all(('_ch' in n and '_read' in n) for n in read_names):
        counter = collections.defaultdict(int)
        for n in read_names:
            nickname = 'ch' + n.split('_ch')[-1].split('_strand')[0]
            counter[nickname] += 1
        for n in read_names:
            nickname = 'ch' + n.split('_ch')[-1].split('_strand')[0]
            if counter[nickname] == 1:
                nickname_dict[n] = nickname
            else:
                nickname_dict[n] = n[prefix:]
        return nickname_dict

    # If the above failed, just trim off any common prefix.
    if prefix > 0:
        return {n: n[prefix:] for n in read_names}
    else:
        return {n: n for n in read_names}
