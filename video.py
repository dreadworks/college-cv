#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""

Script to detect yield signs from video.  Videos are loaded into ram,
so you need to split the file or else it starts swapping ;)

"""


import sys
import argparse
import configparser

import numpy as np
import skvideo.io as skvio
import skimage.draw as skd
from tqdm import tqdm


from src import video
from src import logger
from src import tmeasure

# ---


ROI_LIFESPAN = 30

log = logger(name=__name__[2:-2])
cfg = configparser.ConfigParser()


# ---


def load(fname: str) -> np.ndarray:
    vid = skvio.vread(fname)
    n, h, w, depth = vid.shape
    if depth != 3:
        print('You need to provide a colored video!')
        sys.exit(2)

    log.info('loaded video with %d frames and resulution %d, %d',
             n, h, w)

    return vid


def save(fname: str, vid: np.ndarray):
    log.info('saving to %s', fname)
    skvio.vwrite(fname, vid)


def _draw_vertices(frame, roi):
    rr, cc = skd.polygon_perimeter(
        roi.vy + (roi.vy[0], ),
        roi.vx + (roi.vx[0], ),
        shape=frame.shape)

    frame[rr, cc] = [int(255 * roi.health / ROI_LIFESPAN), 0, 0]


def _draw_roi(arr, i, roi, val):
    y0, y1 = roi.r0 - 1, roi.r1
    x0, x1 = roi.c0 - 1, roi.c1

    # clockwise from topleft
    rr, cc = skd.polygon_perimeter(
        (y0, y0, y1, y1, y0),
        (x0, x1, x1, x0, x0),
        shape=arr[i].shape)

    arr[i, rr, cc] = val

    # color indicator for life decay
    rr, cc = skd.circle(y0, x0, 5, shape=arr[i].shape)
    arr[i, rr, cc] = val * (roi.health / ROI_LIFESPAN)
    rr, cc = skd.circle_perimeter(y0, x0, 5)
    arr[i, rr, cc] = val


def _get_vertices(keys, pois):
    p0, p1, p2 = keys
    vertices = (p0, p1), (p0, p2), (p1, p2)
    vy, vx = zip(*[pois[a][b] for a, b in vertices])
    return vy, vx


def _find_rois(buf, frame, barycenters, pois):
    _, w, h, _ = buf.original.shape
    rois = []

    for keys, (y, x) in barycenters:
        vy, vx = _get_vertices(keys, pois)
        rois.append(video.ROI(w, h, y, x, vy, vx, ROI_LIFESPAN))

    return rois


def _scan_full(buf, frame, i, pipe):
    segments = pipe.binarize(frame.astype(np.int64))

    buf.binary[i] = segments
    if pipe.binary and not pipe.edges:
        return []

    edges = pipe.edge(segments)

    buf.edges[i] = edges

    if pipe.edges:
        return []

    barycenters, pois = pipe.detect(edges, segments)
    rois = _find_rois(buf, frame, barycenters, pois)

    return rois


def _scan_roi(buf, frame, i, pipe, roi):
    r0, r1, c0, c1 = roi.r0, roi.r1, roi.c0, roi.c1

    buf.binary[i, r0:r1, c0:c1] = pipe.binarize(
        frame[r0:r1, c0:c1].astype(np.int64))

    _draw_roi(buf.binary, i, roi, 255)

    buf.edges[i, r0:r1, c0:c1] = pipe.edge(
        buf.binary[i, r0:r1, c0:c1])

    _draw_roi(buf.edges, i, roi, 255)

    return pipe.detect(
        buf.edges[i, r0:r1, c0:c1],
        buf.binary[i, r0:r1, c0:c1])


def _merge_rois(w, h, roi, barycenter, pois, new_rois):
    keys, (y, x) = barycenter
    vy, vx = _get_vertices(keys, pois)
    vy = tuple(map(lambda y: roi.r0 + y, vy))
    vx = tuple(map(lambda x: roi.c0 + x, vx))

    new_roi = video.ROI(w, h, y, x, vy, vx, ROI_LIFESPAN)
    if not any([new_roi.intersects(r) for r in new_rois]):
        new_rois.append(new_roi)


def _process(buf, frame, i, pipe, rois):
    _, w, h, _ = buf.original.shape
    new_rois = []

    # full scan
    rescan = i % 15 == 0
    no_rois = len(rois) == 0
    if rescan or no_rois:
        new_rois += _scan_full(buf, frame, i, pipe)

    # roi scan
    for roi in rois:
        barycenters, pois = _scan_roi(buf, frame, i, pipe, roi)
        if len(barycenters) > 0:
            _merge_rois(w, h, roi, list(barycenters)[0], pois, new_rois)
        else:
            if not roi.dead:
                roi.punish()
                new_rois.append(roi)

    # nice drawings...
    frame //= 3
    for roi in new_rois:
        _draw_vertices(frame, roi)
        _draw_roi(buf.original, i, roi, np.array([255, 255, 255]))

    return new_rois


def process(vid: np.ndarray, only, config=None) -> video.Buffer:
    only_binary, only_edges = only
    only_binary = only_edges or only_binary

    log.info('initializing modules')

    pipe = video.Pipeline(config, only_binary, only_edges)
    buf = video.Buffer(vid)

    log.info('start processing')

    n, w, h, _ = vid.shape
    rois = []

    print('')
    done = tmeasure(log.info, 'took %sms')
    stats = []

    for i, frame in tqdm(buf, total=buf.framecount, unit='frames'):
        stats.append(len(rois) if len(rois) > 0 else '.')
        rois = _process(buf, frame, i, pipe, rois)

    print(''.join(map(str, stats)) + '\n')
    done()

    return buf

#
# --- initialization
#


def parse_args():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        'f_in', type=str,
        help='input file')

    parser.add_argument(
        'f_out', type=str,
        help='output file')

    parser.add_argument(
        '--config', type=str, nargs=1,
        help='configuration file')

    parser.add_argument(
        '--binary',
        action='store_true',
        help='only apply segmentation and morphology')

    parser.add_argument(
        '--edges',
        action='store_true',
        help='only apply --binary and edge detection')

    parser.add_argument(
        '--save-all',
        action='store_true',
        help='save not only the result but all intermediate steps')

    args = parser.parse_args()

    if args.binary and args.edges:
        print('either provide --edges or --binary')
        parser.print_help()
        sys.exit(2)

    if args.save_all and (args.binary or args.edges):
        print('you can not save_all when using --edges or --binary')
        parser.print_help()
        sys.exit(2)

    return args


def _save(buf, args):
    if args.save_all:
        out = args.f_out.rsplit('.', maxsplit=1)

        out.insert(-1, 'binary')
        save('.'.join(out), buf.binary)

        out[-2] = 'edges'
        save('.'.join(out), buf.edges)

        save(args.f_out, buf.original)
        return

    if args.edges:
        save(args.f_out, buf.edges)
        return

    if args.binary:
        save(args.f_out, buf.binary)
        return

    save(args.f_out, buf.original)


def main(args):
    log.info('starting the application')

    if len(args.config) > 0:
        cfg.read(args.config[0])
        if 'options' not in cfg:
            print('You need to provide an [option] section')
            sys.exit(2)

    buf = process(load(args.f_in),
                  (args.binary, args.edges),
                  config=cfg if args.config else None)

    _save(buf, args)


if __name__ == '__main__':
    args = parse_args()
    main(args)
