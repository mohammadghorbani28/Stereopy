#######################
# intensity seg
# network infer
#######################

import os
import copy

import cv2
import tifffile
import numpy as np
from skimage import measure

from utils import stlog
import tissueCut_utils.tissue_seg_bcdu as bcdu
import tissueCut_utils.tissue_seg_utils as util

np.random.seed(123)

class tissueCut(object):
    def __init__(self, path, out_path, type, deep, conf):

        self.path = path
        self.type = type  # image type
        self.deep = deep  # segmentation method
        self.out_path = out_path
        self.conf = conf
        stlog.info('image type: %s' % ('ssdna' if type else 'RNA'))
        stlog.info('using method: %s' % ('deep learning' if deep else 'intensity segmentation'))
        # init property
        self.img = []
        self.shape = []
        self.img_thumb = []
        self.mask_thumb = []
        self.mask = []
        self.file = []
        self.file_name = []
        self.file_ext = []

        self._preprocess_file(path)

        self.is_init_bcdu = False
        self.oj_bcdu = None

    # parse file name
    def _preprocess_file(self, path):

        if os.path.isdir(path):
            self.path = path
            file_list = os.listdir(path)
            self.file = file_list
            self.file_name = [os.path.splitext(f)[0] for f in file_list]
            self.file_ext = [os.path.splitext(f)[1] for f in file_list]
        else:
            self.path = os.path.split(path)[0]
            self.file = [os.path.split(path)[-1]]
            self.file_name = [os.path.splitext(self.file[0])[0]]
            self.file_ext = [os.path.splitext(self.file[0])[-1]]

    # RNA image bin
    def _bin(self, img):

        if img.dtype == 'uint8':
            print(img.dtype)
            bin_size = 20
        else:
            print('16', img.dtype)
            bin_size = 200

        kernel = np.zeros((bin_size, bin_size), dtype=np.uint8)
        kernel += 1
        img_bin = cv2.filter2D(img, -1, kernel)

        return img_bin

    def transfer_16bit_to_8bit(self, image_16bit):
        min_16bit = np.min(image_16bit)
        max_16bit = np.max(image_16bit)

        image_8bit = np.array(np.rint(255 * ((image_16bit - min_16bit) / (max_16bit - min_16bit))), dtype=np.uint8)

        return image_8bit

    def resize(self, img, l=512):
        h, w = img.shape[:2]
        ratio = l / max(h, w)
        show_h = int(h * ratio)
        show_w = int(w * ratio)
        img_out = cv2.resize(img, (show_w, show_h))
        return img_out

    def ij_auto_contrast(self, img):
        limit = img.size / 10
        threshold = img.size / 5000
        if img.dtype != 'uint8':
            bit_max = 65536
        else:
            bit_max = 256
        hist, _ = np.histogram(img.flatten(), 256, [0, bit_max])
        hmin = 0
        hmax = bit_max - 1
        for i in range(1, len(hist) - 1):
            count = hist[i]
            if count > limit:
                continue
            if count > threshold:
                hmin = i
                break
        for i in range(len(hist) - 2, 0, -1):
            count = hist[i]
            if count > limit:
                continue
            if count > threshold:
                hmax = i
                break
        dst = copy.deepcopy(img)
        if hmax > hmin:
            hmax = int(hmax * bit_max / 256)
            hmin = int(hmin * bit_max / 256)
            dst[dst < hmin] = hmin
            dst[dst > hmax] = hmax
            cv2.normalize(dst, dst, 0, bit_max - 1, cv2.NORM_MINMAX)
        return dst

    def save_tissue_mask(self):

        # for idx, tissue_thumb in enumerate(self.mask_thumb):
        #     tifffile.imsave(os.path.join(self.out_path, self.file_name[idx] + r'_tissue_cut_thumb.tif'), tissue_thumb)
        for idx, tissue in enumerate(self.mask):
            if np.sum(tissue) == 0:
                h, w = tissue.shape[:2]
                tissue = np.ones((h, w), dtype=np.uint8)
                tifffile.imsave(os.path.join(self.out_path, self.file_name[idx] + r'_tissue_cut.tif'), tissue)
            else:
                tifffile.imsave(os.path.join(self.out_path, self.file_name[idx] + r'_tissue_cut.tif'),
                                (tissue > 0).astype(np.uint8))
        stlog.info('seg results saved in %s' % self.out_path)

    # preprocess image for deep learning
    def get_thumb_img(self):
        stlog.info('image loading and preprocessing...')

        for ext, file in zip(self.file_ext, self.file):
            assert ext in ['.tif', '.tiff', '.png', '.jpg']
            if ext == '.tif' or ext == '.tiff':

                img = tifffile.imread(os.path.join(self.path, file))
                img = np.squeeze(img)
                if len(img.shape) == 3:
                    img = img[:, :, 0]
            else:

                img = cv2.imread(os.path.join(self.path, file), 0)

            self.img.append(img)

            self.shape.append(img.shape)

            if self.deep:
                self.img_thumb.append(img)

    # tissue segmentation by intensity filter
    def tissue_seg_intensity(self):

        def getArea(elem):
            return elem.area

        self.get_thumb_img()

        stlog.info('segment by intensity...')
        for idx, ori_image in enumerate(self.img):
            shapes = ori_image.shape

            # downsample ori_image
            if not self.type:
                ori_image = self._bin(ori_image)

            image_thumb = util.down_sample(ori_image, shape=(shapes[0] // 5, shapes[1] // 5))

            if image_thumb.dtype != 'uint8':
                image_thumb = util.transfer_16bit_to_8bit(image_thumb)

            self.img_thumb.append(image_thumb)

            # binary
            ret1, mask_thumb = cv2.threshold(image_thumb, 125, 255, cv2.THRESH_OTSU)

            kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (20, 20))  # 椭圆结构
            mask_thumb = cv2.morphologyEx(mask_thumb, cv2.MORPH_CLOSE, kernel, iterations=8)

            # choose tissue prop
            label_image = measure.label(mask_thumb, connectivity=2)
            props = measure.regionprops(label_image, intensity_image=mask_thumb)
            props.sort(key=getArea, reverse=True)
            areas = [p['area'] for p in props]
            if np.std(areas) * 10 < np.mean(areas):
                label_num = len(areas)
            else:
                label_num = int(np.sum(areas >= np.mean(areas)))
            result = np.zeros((image_thumb.shape)).astype(np.uint8)
            for i in range(label_num):
                prop = props[i]
                result += np.where(label_image != prop.label, 0, 1).astype(np.uint8)

            result_thumb = util.hole_fill(result)
            # result_thumb = cv2.dilate(result_thumb, kernel, iterations=10)

            self.mask_thumb.append(np.uint8(result_thumb > 0))

        self.__get_roi()

    # filter noise tissue
    def __filter_roi(self, props):

        if len(props) == 1:
            return props
        else:
            filtered_props = []
            for id, p in enumerate(props):

                black = np.sum(p['intensity_image'] == 0)
                sum = p['bbox_area']
                ratio_black = black / sum
                pixel_light_sum = np.sum(np.unique(p['intensity_image']) > 128)
                if ratio_black < 0.75 and pixel_light_sum > 20:
                    filtered_props.append(p)
            return filtered_props

    def __get_roi(self):

        """get tissue area from ssdna"""
        for idx, tissue_mask in enumerate(self.mask_thumb):

            label_image = measure.label(tissue_mask, connectivity=2)
            props = measure.regionprops(label_image, intensity_image=self.img_thumb[idx])

            # remove noise tissue mask
            filtered_props = self.__filter_roi(props)
            if len(props) != len(filtered_props):
                tissue_mask_filter = np.zeros((tissue_mask.shape), dtype=np.uint8)
                for tissue_tile in filtered_props:
                    bbox = tissue_tile['bbox']
                    tissue_mask_filter[bbox[0]: bbox[2], bbox[1]: bbox[3]] += tissue_tile['image']
                self.mask_thumb[idx] = np.uint8(tissue_mask_filter > 0)
            self.mask.append(util.up_sample(self.mask_thumb[idx], self.img[idx].shape))

    def tissue_infer_bcud(self):
        stlog.info('tissueCut_model infer...')
        if not self.is_init_bcdu:
            self.oj_bcdu = bcdu.cl_bcdu(os.path.join(
                os.path.split(__file__)[0],
                '../tissueCut_model/weight_tissue_cut_tool_220304.hdf5'))
            self.is_init_bcdu = True
        self.get_thumb_img()
        if self.oj_bcdu is not None:
            for img in self.img_thumb:
                try:
                    ret, pred, score = self.oj_bcdu.predict(img)
                except:
                    stlog.info("TissueCut predict error, Please check fov_stitched_transformed.tif")
                    raise Exception('SAW-A40007', "TissueCut predict error")
                if ret:
                    self.mask.append(pred)

    def tissue_seg(self):

        # try:
        if self.deep:
            # self.tissue_infer_deep()
            self.tissue_infer_bcud()
        else:
            self.tissue_seg_intensity()

        self.save_tissue_mask()
