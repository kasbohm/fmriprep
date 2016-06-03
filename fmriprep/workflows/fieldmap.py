#!/usr/bin/env python
# -*- coding: utf-8 -*-
# emacs: -*- mode: python; py-indent-offset: 4; indent-tabs-mode: nil -*-
# vi: set ft=python sts=4 ts=4 sw=4 et:
"""
Fieldmap-processing workflows.

Originally coded by Craig Moodie. Refactored by the CRN Developers.
"""

import os.path as op

from nipype.interfaces import fsl
from nipype.interfaces import io as nio
from nipype.interfaces import utility as niu
from nipype.interfaces.ants.segmentation import N4BiasFieldCorrection
from nipype.pipeline import engine as pe

from ..viz import stripped_brain_overlay


def se_pair_workflow(name='Fieldmap_SEs', settings=None):  # pylint: disable=R0914
    """
    Estimates the fieldmap using TOPUP on series of :abbr:`SE (Spin-Echo)` images
    acquired with varying :abbr:`PE (phase encoding)` direction.
    """

    if settings is None:
        settings = {}

    dwell_time = settings['epi'].get('dwell_time', 0.000700012460221792)
    workflow = pe.Workflow(name=name)

    inputnode = pe.Node(niu.IdentityInterface(fields=['fieldmaps', 'fieldmaps_meta',
                        'sbref']), name='inputnode')
    outputnode = pe.Node(niu.IdentityInterface(fields=['out_field', 'fmap_rads', 'fmap_mask',
                        'mag_brain', 'out_topup', 'fmap_unmasked']), name='outputnode')


    create_parameters_node = pe.Node(interface=niu.Function(
        input_names=['fieldmaps', 'fieldmaps_meta'], output_names=['parameters_file'],
        function=create_encoding_file), name='Create_Parameters', updatehash=True)

    fslmerge = pe.Node(fsl.Merge(dimension='t'), name='SE_merge')
    hmc_se = pe.Node(fsl.MCFLIRT(), name='SE_head_motion_corr')
    fslsplit = pe.Node(fsl.Split(dimension='t'), name='SE_split')

    # Run topup to estimate field distortions
    topup = pe.Node(fsl.TOPUP(), name='TopUp')

    # Use the least-squares method to correct the dropout of the SE images
    unwarp_mag = pe.Node(fsl.ApplyTOPUP(in_index=[1], method='lsr'), name='TopUpApply')

    # Remove bias
    inu_n4 = pe.Node(N4BiasFieldCorrection(dimension=3), name='SE_bias')

    # Skull strip corrected SE image to get reference brain and mask
    mag_bet = pe.Node(fsl.BET(mask=True, robust=True), name='SE_brain')

    # Convert topup fieldmap to rad/s [ 1 Hz = 6.283 rad/s]
    fmap_scale = pe.Node(fsl.BinaryMaths(operation='mul', operand_value=6.283),
                         name='fmap_scale')

    # Compute a mask from the fieldmap (??)
    fmap_abs = pe.Node(fsl.UnaryMaths(operation='abs', args='-bin'), name='fmap_abs')
    fmap_mul = pe.Node(fsl.BinaryMaths(operation='mul'), name='fmap_mul_mask')

    # Compute an smoothed field without mask
    # TODO: unwarp_direction, dwell_time dynamically set
    # TODO: IMHO this should disappear
    fugue_unmask = pe.Node(fsl.FUGUE(unwarp_direction='x', dwell_time=dwell_time,
                                     save_unmasked_fmap=True), name='fmap_unmask')

    workflow.connect([
        (inputnode, fslmerge, [('fieldmaps', 'in_files')]),
        (fslmerge, hmc_se, [('merged_file', 'in_file')]),
        (inputnode, hmc_se, [('sbref', 'ref_file')]),
        (inputnode, create_parameters_node, [('fieldmaps', 'fieldmaps'),
                                        ('fieldmaps_meta', 'fieldmaps_meta')]),
        (create_parameters_node, topup, [('parameters_file', 'encoding_file')]),
        (hmc_se, topup, [('out_file', 'in_file')]),
        (topup, unwarp_mag, [('out_fieldcoef', 'in_topup_fieldcoef'),
                             ('out_movpar', 'in_topup_movpar')]),
        (create_parameters_node, unwarp_mag, [('parameters_file', 'encoding_file')]),
        (hmc_se, fslsplit, [('out_file', 'in_file')]),
        (fslsplit, unwarp_mag, [('out_files', 'in_files')]),
        (unwarp_mag, inu_n4, [('out_corrected', 'input_image')]),
        (inu_n4, mag_bet, [('output_image', 'in_file')]),
        (topup, fmap_scale, [('out_field', 'in_file')]),
        (mag_bet, fmap_mul, [('mask_file', 'operand_file')]),

        (fmap_scale, fmap_abs, [('out_file', 'in_file')]),
        (fmap_abs, fmap_mul, [('out_file', 'in_file')]),
        (fmap_scale, fugue_unmask, [('out_file', 'fmap_in_file')]),
        (fmap_mul, fugue_unmask, [('out_file', 'mask_file')]),
        (topup, outputnode, [('out_field', 'out_field')]),
        (fmap_scale, outputnode, [('out_file', 'fmap_rads')]),
        (mag_bet, outputnode, [('out_file', 'mag_brain'),
                               ('mask_file', 'fmap_mask')]),
        (unwarp_mag, outputnode, [('out_corrected', 'out_topup')]),
        (fugue_unmask, outputnode, [('fmap_out_file', 'fmap_unmasked')])
    ])

    # Reports section
    se_png = pe.Node(niu.Function(
        input_names=['in_file', 'overlay_file', 'out_file'], output_names=['out_file'],
        function=stripped_brain_overlay), name='PNG_SE_corr')
    se_png.inputs.out_file = 'corrected_SE_and_mask.png'

    datasink = pe.Node(nio.DataSink(base_directory=op.join(settings['work_dir'], 'images')),
                       name='datasink', parameterization=False)
    workflow.connect([
        (unwarp_mag, se_png, [('out_corrected', 'overlay_file')]),
        (mag_bet, se_png, [('mask_file', 'in_file')]),
        (se_png, datasink, [('out_file', '@corrected_SE_and_mask')])
    ])

    return workflow

def create_encoding_file(fieldmaps, fieldmaps_meta):
    """Creates a valid encoding file for topup"""
    import json
    import nibabel as nb
    import os

    with open('parameters.txt', 'w') as parameters_file:
        for fieldmap, fieldmap_meta in zip(fieldmaps, fieldmaps_meta):
            meta = json.load(open(fieldmap_meta))
            pedir = {'i': 0, 'j': 1, 'k': 2}
            line_values = [0, 0, 0, meta['TotalReadoutTime']]
            line_values[pedir[meta['PhaseEncodingDirection'][0]]] = 1 + (-2*(len(meta['PhaseEncodingDirection']) == 2))
            for i in range(nb.load(fieldmap).shape[-1]):
                parameters_file.write(
                    ' '.join([str(i) for i in line_values]) + '\n')
    return os.path.abspath('parameters.txt')
