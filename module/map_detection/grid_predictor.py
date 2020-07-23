from module.base.utils import *
from module.config.config import AzurLaneConfig
from module.map_detection.utils import *
from module.template.assets import *


class GridPredictor:
    DIC_ENEMY_GENRE = {
        'Light': TEMPLATE_ENEMY_LIGHT,
        'Main': TEMPLATE_ENEMY_MAIN,
        'Carrier': TEMPLATE_ENEMY_CARRIER,
        'Treasure': TEMPLATE_ENEMY_TREASURE,
    }
    SIREN_TEMPLATE_LOADED = False

    def __init__(self, location, image, corner, config):
        """
        Args:
            location (tuple): Grid location, (x, y).
            image (np.ndarray): Shape (720, 1280, 3)
            corner (np.ndarray): shape (4, 2), [upper-left, upper-right, bottom-left, bottom-right]
            config (AzurLaneConfig):
        """
        self.location = location
        self.image = image
        self.corner = corner
        self.config = config

        # Calculate directly is faster than calling existing functions.
        x0, y0, x1, y1, x2, y2, x3, y3 = corner.flatten()
        divisor = x0 - x1 + x2 - x3
        x = (x0 * x2 - x1 * x3) / divisor
        y = (x0 * y2 - x1 * y2 + x2 * y0 - x3 * y0) / divisor
        self._image_center = np.array([x, y, x, y])
        self._image_a = (-x0 * x2 + x0 * x3 + x1 * x2 - x1 * x3) / divisor

        self.area = corner2area(self.corner)
        self.homo_data = cv2.getPerspectiveTransform(
            src=self.corner.astype(np.float32),
            dst=area2corner((0, 0, *self.config.HOMO_TILE)).astype(np.float32))
        self.homo_invt = cv2.invert(self.homo_data)[1]

    def screen2grid(self, points):
        return perspective_transform(points, self.homo_data) / self.config.HOMO_TILE

    def grid2screen(self, points):
        return perspective_transform(np.multiply(points, self.config.HOMO_TILE), self.homo_invt)

    @cached_property
    def image_trans(self):
        return cv2.warpPerspective(self.image, self.homo_data, self.config.HOMO_TILE)

    @cached_property
    def image_homo(self):
        image_edge = cv2.Canny(rgb2gray(self.image_trans), 100, 150)
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        image_edge = cv2.morphologyEx(image_edge, cv2.MORPH_CLOSE, kernel)
        return image_edge

    def predict(self):
        self.enemy_scale = self.predict_enemy_scale()
        self.enemy_genre = self.predict_enemy_genre()
        self.is_boss = self.predict_boss()
        self.is_fleet = self.predict_fleet()
        self.is_mystery = self.predict_mystery()
        self.is_current_fleet = self.predict_current_fleet()

        if self.enemy_genre:
            self.is_enemy = True
        if self.enemy_scale:
            self.is_enemy = True
        # if not self.is_enemy:
        #     self.is_enemy = self.predict_static_red_border()
        if self.is_enemy and not self.enemy_genre:
            self.enemy_genre = 'Enemy'
        if self.config.MAP_HAS_SIREN:
            if self.enemy_genre is not None and self.enemy_genre.startswith('Siren'):
                self.is_siren = True
                self.enemy_scale = 0

    def relative_crop(self, area, shape=None):
        """Crop image and rescale to target shape. Eliminate the effect of perspective.

        Args:
            area (tuple): upper_left_x, upper_left_y, bottom_right_x, bottom_right_y, such as (-1, -1, 1, 1).
            shape (tuple): Output image shape, (width, height).

        Returns:
            np.ndarray: Shape (height, width, channel).
        """
        area = self._image_center + np.array(area) * self._image_a
        image = crop(self.image, area=np.rint(area).astype(int))
        if shape is not None:
            # Follow the default re-sampling filter in pillow, which is BICUBIC.
            image = cv2.resize(image, shape, interpolation=cv2.INTER_CUBIC)
        return image

    def relative_rgb_count(self, area, color, shape=(50, 50), threshold=221):
        """
        Args:
            area (tuple): upper_left_x, upper_left_y, bottom_right_x, bottom_right_y, such as (-1, -1, 1, 1).
            color (tuple): Target RGB.
            shape (tuple): Output image shape, (width, height).
            threshold (int): 0-255. The bigger, the more similar. 255 means the same color.

        Returns:
            int: Number of matched pixels.
        """
        image = color_similarity_2d(self.relative_crop(area, shape=shape), color=color)
        count = image[image > threshold].shape[0]
        return count

    def relative_hsv_count(self, area, h=(0, 360), s=(0, 100), v=(0, 100), shape=(50, 50)):
        """
        Args:
            area (tuple): upper_left_x, upper_left_y, bottom_right_x, bottom_right_y, such as (-1, -1, 1, 1).
            h (tuple): Hue.
            s (tuple): Saturation.
            v (tuple): Value.
            shape (tuple): Output image shape, (width, height).

        Returns:
            int: Number of matched pixels.
        """
        image = cv2.cvtColor(self.relative_crop(area, shape=shape), cv2.COLOR_RGB2HSV)
        lower = (h[0] / 2, s[0] * 2.55, v[0] * 2.55)
        upper = (h[1] / 2 + 1, s[1] * 2.55 + 1, v[1] * 2.55 + 1)
        image = cv2.inRange(image, lower, upper)
        count = image[image > 0].shape[0]
        return count

    def predict_enemy_scale(self):
        """
        Detect the icon on the upper-left which shows enemy scale: Large, Middle, Small.

        Returns:
            int: 1: Small, 2: Middle, 3: Large, 0: Unknown.
        """
        image = self.relative_crop((-0.415 - 0.7, -0.62 - 0.7, -0.415, -0.62), shape=(50, 50))
        red = color_similarity_2d(image, (255, 130, 132))
        yellow = color_similarity_2d(image, (255, 235, 156))

        if TEMPLATE_ENEMY_L.match(red):
            scale = 3
        elif TEMPLATE_ENEMY_M.match(yellow):
            scale = 2
        elif TEMPLATE_ENEMY_S.match(yellow):
            scale = 1
        else:
            scale = 0

        return scale

    def predict_enemy_genre(self):
        image = rgb2gray(self.relative_crop((-0.5, -1, 0.5, 0), shape=(60, 60)))
        if not self.SIREN_TEMPLATE_LOADED:
            for name in self.config.MAP_SIREN_TEMPLATE:
                self.DIC_ENEMY_GENRE[f'Siren_{name}'] = globals().get(f'TEMPLATE_SIREN_{name}')
                self.SIREN_TEMPLATE_LOADED = True

        for name, template in self.DIC_ENEMY_GENRE.items():
            if not self.config.MAP_HAS_SIREN and name.startswith('Siren'):
                continue
            if template.match(image):
                return name

        return None

    def predict_boss(self):
        image = self.relative_crop((-0.55, -0.2, 0.45, 0.2), shape=(50, 20))
        image = color_similarity_2d(image, color=(255, 77, 82))
        if TEMPLATE_ENEMY_BOSS.match(image, similarity=0.75):
            return True

        # Small boss icon
        if self.relative_hsv_count(area=(0.03, -0.15, 0.63, 0.15), h=(358 - 3, 358 + 3), shape=(50, 20)) > 100:
            image = self.relative_crop((0.03, -0.15, 0.63, 0.15), shape=(50, 20))
            image = color_similarity_2d(image, color=(255, 77, 82))
            if TEMPLATE_ENEMY_BOSS.match(image, similarity=0.7):
                return True

        return False

    def predict_fleet(self):
        image = self.relative_crop((-1, -2, -0.5, -1.5), shape=(50, 50))
        image = color_similarity_2d(image, color=(255, 255, 255))
        return TEMPLATE_FLEET_AMMO.match(image)

    def predict_mystery(self):
        """
        Returns:
            bool: True if is mystery.
        """
        # cyan question mark
        if self.relative_rgb_count(
                area=(-0.3, -2, 0.3, -0.6), color=(148, 255, 247), shape=(20, 50)) > 50:
            return True
        # white background
        # if self.relative_rgb_count(
        #         area=(-0.7, -1.7, 0.7, -0.3), color=(239, 239, 239), shape=(50, 50)) > 700:
        #     return True

        return False

    def predict_current_fleet(self):
        count = self.relative_hsv_count(area=(-0.5, -3.5, 0.5, -2.5), h=(141 - 3, 141 + 10), shape=(50, 50))
        return count > 600

    def predict_sea(self):
        area = area_pad((48, 48, 48 + 46, 48 + 46), pad=5)
        res = cv2.matchTemplate(ASSETS.tile_center_image, crop(self.image_homo, area=area), cv2.TM_CCOEFF_NORMED)
        _, sim, _, _ = cv2.minMaxLoc(res)
        if sim > 0.8:
            return True

        tile = 135
        corner = 25
        corner = [(5, 5, corner, corner), (tile - corner, 5, tile, corner), (5, tile - corner, corner, tile),
                  (tile - corner, tile - corner, tile, tile)]
        for area, template in zip(corner[::-1], ASSETS.tile_corner_image_list[::-1]):
            res = cv2.matchTemplate(template, crop(self.image_homo, area=area), cv2.TM_CCOEFF_NORMED)
            _, sim, _, _ = cv2.minMaxLoc(res)
            if sim > 0.8:
                return True

        return False