# --------------------------------------------------------
# Fast R-CNN
# Copyright (c) 2015 Microsoft
# Licensed under The MIT License [see LICENSE for details]
# Written by Ross Girshick
# --------------------------------------------------------

"""Test a Fast R-CNN network on an imdb (image database)."""

from fast_rcnn.config import cfg, get_output_dir
import argparse
from utils.timer import Timer
import numpy as np
import cv2
import caffe
from utils.cython_nms import nms
import cPickle
import heapq
from utils.blob import im_list_to_blob
import os
import PIL

def _get_image_blob(im):
    """Converts an image into a network input.

    Arguments:
        im (ndarray): a color image in BGR order

    Returns:
        blob (ndarray): a data blob holding an image pyramid
        im_scale_factors (list): list of image scales (relative to im) used
            in the image pyramid
    """
    im_orig = im.astype(np.float32, copy=True)
    im_orig -= cfg.PIXEL_MEANS

    im_shape = im_orig.shape
    im_size_min = np.min(im_shape[0:2])
    im_size_max = np.max(im_shape[0:2])

    processed_ims = []
    im_scale_factors = []

    for target_size in cfg.TEST.SCALES:
        im_scale = float(target_size) / float(im_size_min)
        # Prevent the biggest axis from being more than MAX_SIZE
        if np.round(im_scale * im_size_max) > cfg.TEST.MAX_SIZE:
            im_scale = float(cfg.TEST.MAX_SIZE) / float(im_size_max)
        im = cv2.resize(im_orig, None, None, fx=im_scale, fy=im_scale,
                        interpolation=cv2.INTER_LINEAR)
        im_scale_factors.append(im_scale)
        processed_ims.append(im)

    # Create a blob to hold the input images
    blob = im_list_to_blob(processed_ims)

    return blob, np.array(im_scale_factors)

def _get_rois_blob(im_rois, im_scale_factors):
    """Converts RoIs into network inputs.

    Arguments:
        im_rois (ndarray): R x 4 matrix of RoIs in original image coordinates
        im_scale_factors (list): scale factors as returned by _get_image_blob

    Returns:
        blob (ndarray): R x 5 matrix of RoIs in the image pyramid
    """
    rois, levels = _project_im_rois(im_rois, im_scale_factors)
    rois_blob = np.hstack((levels, rois))
    return rois_blob.astype(np.float32, copy=False)

def _project_im_rois(im_rois, scales):
    """Project image RoIs into the image pyramid built by _get_image_blob.

    Arguments:
        im_rois (ndarray): R x 4 matrix of RoIs in original image coordinates
        scales (list): scale factors as returned by _get_image_blob

    Returns:
        rois (ndarray): R x 4 matrix of projected RoI coordinates
        levels (list): image pyramid levels used by each projected RoI
    """
    im_rois = im_rois.astype(np.float, copy=False)

    if len(scales) > 1:
        widths = im_rois[:, 2] - im_rois[:, 0] + 1
        heights = im_rois[:, 3] - im_rois[:, 1] + 1

        areas = widths * heights
        scaled_areas = areas[:, np.newaxis] * (scales[np.newaxis, :] ** 2)
        diff_areas = np.abs(scaled_areas - 224 * 224)
        levels = diff_areas.argmin(axis=1)[:, np.newaxis]
    else:
        levels = np.zeros((im_rois.shape[0], 1), dtype=np.int)

    rois = im_rois * scales[levels]

    return rois, levels

def _get_blobs(im, rois):
    """Convert an image and RoIs within that image into network inputs."""
    blobs = {'data' : None, 'rois' : None}
    blobs['data'], im_scale_factors = _get_image_blob(im)
    blobs['rois'] = _get_rois_blob(rois, im_scale_factors)
    return blobs, im_scale_factors

def _bbox_pred(boxes, box_deltas):
    """Transform the set of class-agnostic boxes into class-specific boxes
    by applying the predicted offsets (box_deltas)
    """
    if boxes.shape[0] == 0:
        return np.zeros((0, box_deltas.shape[1]))

    boxes = boxes.astype(np.float, copy=False)
    widths = boxes[:, 2] - boxes[:, 0] + cfg.EPS
    heights = boxes[:, 3] - boxes[:, 1] + cfg.EPS
    ctr_x = boxes[:, 0] + 0.5 * widths
    ctr_y = boxes[:, 1] + 0.5 * heights

    dx = box_deltas[:, 0::4]
    dy = box_deltas[:, 1::4]
    dw = box_deltas[:, 2::4]
    dh = box_deltas[:, 3::4]

    pred_ctr_x = dx * widths[:, np.newaxis] + ctr_x[:, np.newaxis]
    pred_ctr_y = dy * heights[:, np.newaxis] + ctr_y[:, np.newaxis]
    pred_w = np.exp(dw) * widths[:, np.newaxis]
    pred_h = np.exp(dh) * heights[:, np.newaxis]

    pred_boxes = np.zeros(box_deltas.shape)
    # x1
    pred_boxes[:, 0::4] = pred_ctr_x - 0.5 * pred_w
    # y1
    pred_boxes[:, 1::4] = pred_ctr_y - 0.5 * pred_h
    # x2
    pred_boxes[:, 2::4] = pred_ctr_x + 0.5 * pred_w
    # y2
    pred_boxes[:, 3::4] = pred_ctr_y + 0.5 * pred_h

    return pred_boxes

def _clip_boxes(boxes, im_shape):
    """Clip boxes to image boundaries."""
    # x1 >= 0
    boxes[:, 0::4] = np.maximum(boxes[:, 0::4], 0)
    # y1 >= 0
    boxes[:, 1::4] = np.maximum(boxes[:, 1::4], 0)
    # x2 < im_shape[1]
    boxes[:, 2::4] = np.minimum(boxes[:, 2::4], im_shape[1] - 1)
    # y2 < im_shape[0]
    boxes[:, 3::4] = np.minimum(boxes[:, 3::4], im_shape[0] - 1)
    return boxes

def im_detect(net, im, boxes, feat_file=None, eval_segm=False):
    """Detect object classes in an image given object proposals.

    Arguments:
        net (caffe.Net): Fast R-CNN network to use
        im (ndarray): color image to test (in BGR order)
        boxes (ndarray): R x 4 array of object proposals

    Returns:
        scores (ndarray): R x K array of object class scores (K includes
            background as object category 0)
        boxes (ndarray): R x (4*K) array of predicted bounding boxes
    """
    blobs, unused_im_scale_factors = _get_blobs(im, boxes)

    # When mapping from image ROIs to feature map ROIs, there's some aliasing
    # (some distinct image ROIs get mapped to the same feature ROI).
    # Here, we identify duplicate feature ROIs, so we only compute features
    # on the unique subset.
    if cfg.DEDUP_BOXES > 0:
        v = np.array([1, 1e3, 1e6, 1e9, 1e12])
        hashes = np.round(blobs['rois'] * cfg.DEDUP_BOXES).dot(v)
        _, index, inv_index = np.unique(hashes, return_index=True,
                                        return_inverse=True)
        blobs['rois'] = blobs['rois'][index, :]
        boxes = boxes[index, :]
        
    if feat_file!=None:
        feat=[]
        feat_layer=['fc7']
    else:
        feat_layer=[]
        
    # reshape network inputs
    net.blobs['data'].reshape(*(blobs['data'].shape))
    net.blobs['rois'].reshape(*(blobs['rois'].shape))
    blobs_out = net.forward(data=blobs['data'].astype(np.float32, copy=False),
                            rois=blobs['rois'].astype(np.float32, copy=False),
                            blobs=feat_layer)
                                
    if feat_file!=None:
        feat = blobs_out[feat_layer[0]].copy()

    if eval_segm:
        pass
    
    if cfg.TEST.SVM:
        # use the raw scores before softmax under the assumption they
        # were trained as linear SVMs
        scores = net.blobs['cls_score'].data
    else:
        # use softmax estimated probabilities
        scores = net.blobs['cls_prob'].data#blobs_out['cls_prob']

    if cfg.TEST.BBOX_REG:
        # Apply bounding-box regression deltas
        box_deltas = blobs_out['bbox_pred']
        pred_boxes = _bbox_pred(boxes, box_deltas)
        pred_boxes = _clip_boxes(pred_boxes, im.shape)
    else:
        # Simply repeat the boxes, once for each class
        pred_boxes = np.tile(boxes, (1, scores.shape[1]))

    if cfg.DEDUP_BOXES > 0:
        # Map scores and predictions back to the original set of boxes
        scores = scores[inv_index, :]
        pred_boxes = pred_boxes[inv_index, :]
    if feat_file!=None:
        return scores, pred_boxes, feat
    else:
	return scores, pred_boxes

from utils.cython_bbox import bbox_overlaps

def evalCorLoc2(imdb,nms_dets,overlap=0.5):
    num_classes = len(nms_dets)
    num_images = len(nms_dets[0])
    gt = imdb.gt_roidb()
    pos = np.zeros(imdb.num_classes)
    tot = np.zeros(imdb.num_classes)
    for cls_ind in xrange(num_classes):
        for im_ind in xrange(num_images):
            dets = nms_dets[cls_ind][im_ind]
            if dets == []:
                continue
            if np.all(gt[im_ind]['gt_classes']!=cls_ind):
                continue
            sel = gt[im_ind]['gt_classes'] == cls_ind
            gtdet = (gt[im_ind]['boxes'][sel]).astype(np.float, copy=False)
            dets = dets.astype(np.float, copy=False)
            ovr = bbox_overlaps(gtdet,dets)
            tot[cls_ind] += gtdet.shape[0]
            pos[cls_ind] += np.sum(ovr.max(1)>overlap)
    corloc = pos[1:]/tot[1:]
    return corloc

def evalCorLoc(imdb,nms_dets,overlap=0.5):
    num_classes = len(nms_dets)
    num_images = len(nms_dets[0])
    gt = imdb.gt_roidb()
    pos = np.zeros(imdb.num_classes)
    tot = np.zeros(imdb.num_classes)
    for cls_ind in xrange(1,num_classes):
        for im_ind in xrange(num_images):
            dets = nms_dets[cls_ind][im_ind]
            if dets == []:
                print "Error, no detections!"
                dfsd
                continue
            if np.all(gt[im_ind]['gt_classes']!=cls_ind):
                continue
            sel = gt[im_ind]['gt_classes'] == cls_ind
            gtdet = (gt[im_ind]['boxes'][sel]).astype(np.float, copy=False)
            dets = dets.astype(np.float, copy=False)
            ovr = bbox_overlaps(gtdet,dets[:1])
            tot[cls_ind] += 1#gtdet.shape[0]
            pos[cls_ind] += ovr.max()>=overlap #> or >=
    corloc = pos[1:]/tot[1:]
    return corloc


#def vis_detections(im, class_name, dets, thresh=0.3):
#    """Visual debugging of detections."""
#    import matplotlib.pyplot as plt
#    im = im[:, :, (2, 1, 0)]
#    for i in xrange(np.minimum(10, dets.shape[0])):
#        bbox = dets[i, :4]
#        score = dets[i, -1]
#        if score > thresh:
#            plt.cla()
#            plt.imshow(im)
#            plt.gca().add_patch(
#                plt.Rectangle((bbox[0], bbox[1]),
#                              bbox[2] - bbox[0],
#                              bbox[3] - bbox[1], fill=False,
#                              edgecolor='g', linewidth=3)
#                )
#            plt.title('{}  {:.3f}'.format(class_name, score))
#            #plt.show()
#    plt.show()

def vis_detections(im, class_name, dets, thresh=0.3):
    """Visual debugging of detections."""
    import matplotlib.pyplot as plt
    im = im[:, :, (2, 1, 0)]
    for i in xrange(np.minimum(3, dets.shape[0])):
        bbox = dets[i, :4]
        score = dets[i, -1]
        if score > thresh:
            plt.gca().add_patch(
            plt.Rectangle((bbox[0], bbox[1]),
                          bbox[2] - bbox[0],
                          bbox[3] - bbox[1], fill=False,
                          edgecolor='g', linewidth=3)
            )
            #plt.title('{}  {:.3f}'.format(class_name, score))
            plt.text(bbox[0],bbox[1],'{}  {:.3f}'.format(class_name, score),backgroundcolor=(1,1,1,1))
            #plt.show()
    #plt.show()

def merge_detections(all_boxes,imdb):
    """Apply non-maximum suppression to all predicted boxes output by the
    test_net method.
    """
    num_classes = len(all_boxes)
    num_images = len(all_boxes[0])
    new_boxes = [[[] for _ in xrange(num_images/2)]
                 for _ in xrange(num_classes)]
    for cls_ind in xrange(num_classes):
        #cls_ind=7
        print "Class",cls_ind
        for im_ind in xrange(num_images/2):
            dets_left = all_boxes[cls_ind][im_ind]
            dets_right = all_boxes[cls_ind][im_ind+num_images/2]
            if dets_right == []:
                continue
            width = PIL.Image.open(imdb.image_path_at(im_ind)).size[0]
            if 0:
                if len(dets_left)>0 and len(dets_right)>0:
                    import pylab
                    im = cv2.imread(imdb.image_path_at(im_ind))
                    pylab.figure(0)
                    pylab.clf()
                    pylab.imshow(im)
                    pylab.draw()
                    pylab.show()
                    pylab.figure(1)
                    pylab.clf()
                    pylab.plot([0,width],[0,400]) #ylim((25,250))
                    for l in range(len(dets_left)):
                        if dets_left[l,4]>0.1:
                            x1=dets_left[l,0];x2=dets_left[l,2]
                            y1=dets_left[l,1];y2=dets_left[l,3]
                            pylab.plot([x1,x1,x2,x2,x1],[y1,y2,y2,y1,y1],lw=3)
                    pylab.draw()
                    pylab.show()
                    pylab.figure(2)
                    pylab.clf()
                    pylab.plot([0,width],[0,400]) #ylim((25,250))
                    for l in range(len(dets_right)):
                        if dets_right[l,4]>0.1:
                            x1=dets_right[l,0];x2=dets_right[l,2]
                            y1=dets_right[l,1];y2=dets_right[l,3]
                            pylab.plot([x1,x1,x2,x2,x1],[y1,y2,y2,y1,y1],lw=3)
                    pylab.draw()
                    pylab.show()
                    #raw_input()            
            oldx1 = dets_right[:, 0].copy()
            oldx2 = dets_right[:, 2].copy()
            dets_right[:,0] = width - oldx2 - 1
            dets_right[:,2] = width - oldx1 - 1
            assert (dets_right[:,2] >= dets_right[:,0]).all()        
            new_boxes[cls_ind][im_ind] = np.concatenate((dets_left,dets_right),0)
            if 0:
                if len(dets_right)>0:
                    import pylab
                    pylab.figure(3)
                    pylab.clf()
                    pylab.plot([0,width],[0,400]) #ylim((25,250))
                    for l in range(len(dets_right)):
                        if dets_right[l,4]>0.1:
                            x1=dets_right[l,0];x2=dets_right[l,2]
                            y1=dets_right[l,1];y2=dets_right[l,3]
                            pylab.plot([x1,x1,x2,x2,x1],[y1,y2,y2,y1,y1],lw=3)
                    pylab.draw()
                    pylab.show()
                    raw_input()
    return new_boxes

def apply_nms(all_boxes, thresh):
    """Apply non-maximum suppression to all predicted boxes output by the
    test_net method.
    """
    num_classes = len(all_boxes)
    num_images = len(all_boxes[0])
    nms_boxes = [[[] for _ in xrange(num_images)]
                 for _ in xrange(num_classes)]
    for cls_ind in xrange(num_classes):
        for im_ind in xrange(num_images):
            dets = all_boxes[cls_ind][im_ind]
            if dets == []:
                continue
            keep = nms(dets, thresh)
            if len(keep) == 0:
                continue
            nms_boxes[cls_ind][im_ind] = dets[keep, :].copy()
    return nms_boxes

def test_net(net, imdb ,args):
    """Test a Fast R-CNN network on an image database."""
    num_images = len(imdb.image_index)
    # heuristic: keep an average of 40 detections per class per images prior
    # to NMS
    max_per_set = 40 * num_images #changed from 40
    # heuristic: keep at most 100 detection per class per image prior to NMS
    max_per_image = 100
    # detection thresold for each class (this is adaptively set based on the
    # max_per_set constraint)
    thresh = -np.inf * np.ones(imdb.num_classes)
    # top_scores will hold one minheap of scores per class (used to enforce
    # the max_per_set constraint)
    top_scores = [[] for _ in xrange(imdb.num_classes)]
    # all detections are collected into:
    #    all_boxes[cls][image] = N x 5 array of detections in
    #    (x1, y1, x2, y2, score)
    all_boxes = [[[] for _ in xrange(num_images)]
                 for _ in xrange(imdb.num_classes)]

    output_dir = get_output_dir(imdb, net)
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    flip_str=''
    if args.use_flip:
        flip_str='flip'

    #print "--------",args.reusedet
    itr= args.caffemodel.split('_')[-1].split('.')[0]
    try:
        if not(args.reusedet):
            generate_error
        det_file = os.path.join(output_dir, 'detections%s%s.pkl'%(flip_str,itr))
        with open(det_file, 'rb') as f:
            all_boxes = cPickle.load(f)

    except:
        # timers
        _t = {'im_detect' : Timer(), 'misc' : Timer()}

        roidb = imdb.roidb
        lfeat = []
        for i in xrange(num_images):
            im = cv2.imread(imdb.image_path_at(i))
            if roidb[i]["flipped"]:
                im = im[:,::-1,:]
            _t['im_detect'].tic()
            scores, boxes = im_detect(net, im, roidb[i]['boxes'],args.feat_file,args.eval_segm)
            _t['im_detect'].toc()
            #lfeat.append(feat)

            if args.visdet:
                import pylab
                pylab.figure(1)
                pylab.clf()
                pylab.imshow(im)

            _t['misc'].tic()
            for j in xrange(1, imdb.num_classes):
                inds = np.where((scores[:, j] > thresh[j]) &
                                (roidb[i]['gt_classes'] == 0))[0]
                cls_scores = scores[inds, j]
                cls_boxes = boxes[inds, j*4:(j+1)*4]
                top_inds = np.argsort(-cls_scores)[:max_per_image]
                cls_scores = cls_scores[top_inds]
                cls_boxes = cls_boxes[top_inds, :]
                # push new scores onto the minheap
                for val in cls_scores:
                    heapq.heappush(top_scores[j], val)
                # if we've collected more than the max number of detection,
                # then pop items off the minheap and update the class threshold
                if len(top_scores[j]) > max_per_set:
                    while len(top_scores[j]) > max_per_set:
                        heapq.heappop(top_scores[j])
                        if args.imdb_name!='voc_2007_trainval':#test
                            thresh[j] = top_scores[j][0]

                all_boxes[j][i] = \
                        np.hstack((cls_boxes, cls_scores[:, np.newaxis])) \
                        .astype(np.float32, copy=False)
                        
                if args.visdet:
                    keep = nms(all_boxes[j][i], 0.3)
                    #import pylab
                    #pylab.figure(1)
                    vis_detections(im, imdb.classes[j], all_boxes[j][i][keep, :],0.3)
                    #pylab.draw()
                    #pylab.show()
                    #raw_input()
            _t['misc'].toc()

            print 'im_detect: {:d}/{:d} {:.3f}s {:.3f}s' \
                  .format(i + 1, num_images, _t['im_detect'].average_time,
                          _t['misc'].average_time)
            
            if args.visdet:
                pylab.draw()
                pylab.show()
                raw_input()

        for j in xrange(1, imdb.num_classes):
            for i in xrange(num_images):
                inds = np.where(all_boxes[j][i][:, -1] > thresh[j])[0]
                all_boxes[j][i] = all_boxes[j][i][inds, :]
                
        if args.feat_file!=None:
            print 'Saving the features in ',os.path.join(output_dir,args.feat_file)
            with open(os.path.join(output_dir,args.feat_file), 'wb') as f:
                cPickle.dump(lfeat, f, cPickle.HIGHEST_PROTOCOL)
                
        det_file = os.path.join(output_dir, 'detections%s%s.pkl'%(flip_str,itr))
        with open(det_file, 'wb') as f:
            cPickle.dump(all_boxes, f, cPickle.HIGHEST_PROTOCOL)

    if args.use_flip:
        print "Merging Left Right Detections"
        all_boxes2 = all_boxes
        #all_boxes=merge_detections(all_boxes2,imdb)
        all_boxes=merge_detections(all_boxes2,imdb)
        from datasets.factory import get_imdb
        imdb = get_imdb(args.imdb_name)

    print 'Applying NMS to all detections'
    nms_dets = apply_nms(all_boxes, cfg.TEST.NMS)

    if args.imdb_name=='voc_2007_trainval':
        print 'Evaluate CorLoc'
        corloc=evalCorLoc(imdb,nms_dets)    
        print "CorLoc",corloc
        print "Mean",corloc.mean()
        corlocover=[]
        for l in range(11):
            corlocover.append(evalCorLoc(imdb,nms_dets,overlap=float(l)/10.0).mean())
            print "Overlap",float(l)/10.0,
            print "CorLoc",corlocover[-1]
        if 1:
            import pylab
            pylab.figure()
            pylab.plot(corlocover)
            pylab.plot([0,10],[corlocover[0],corlocover[-1]])
            pylab.show()
    else:
        print 'Evaluating detections'
        imdb.evaluate_detections(nms_dets, output_dir, args.overlap)
