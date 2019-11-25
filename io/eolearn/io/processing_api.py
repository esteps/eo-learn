''' An input task for the `sentinelhub processing api <https://docs.sentinel-hub.com/api/latest/reference/>`
'''
import json
import logging
from copy import deepcopy
import datetime as dt
import numpy as np

from sentinelhub import WebFeatureService, MimeType, SentinelHubDownloadClient, DownloadRequest, SHConfig
import sentinelhub.sentinelhub_request as shr

from eolearn.core import EOPatch, EOTask

LOGGER = logging.getLogger(__name__)


class SentinelHubProcessingInput(EOTask):
    ''' A processing API input task that loads 16bit integer data and converts it to a 32bit float feature.
    '''
    def __init__(self, data_source, size=None, resolution=None, bands_feature=None, bands=None, additional_data=None,
                 maxcc=1.0, time_difference=-1, cache_folder=None, max_threads=5):
        """
        :param size_x: Number of pixels in x dimension.
        :type size_x: int
        :param size_y: Number of pixels in y dimension.
        :type size_y: int
        :param time_range: A range tuple of (date_from, date_to), defining a time range from which to acquire the data.
        :type time_range: tuple(str, str)
        :param bands_feature: Target feature into which to save the downloaded images.
        :type bands_feature: tuple(sentinelhub.FeatureType, str)
        :param bands: An array of band names.
        :type bands: list[str]
        :param additional_data: A list of additional data to be downloaded, such as SCL, SNW, dataMask, etc.
        :type additional_data: list[tuple(sentinelhub.FeatureType, str)]
        :param maxcc: Maximum cloud coverage.
        :type maxcc: float
        :param time_difference: Minimum allowed time difference in minutes, used when filtering dates.
        :type time_difference: int
        :param cache_folder: Path to cache_folder. If set to None (default) requests will not be cached.
        :type cache_folder: str
        :param max_threads: Maximum threads to be used when downloading data.
        :type max_threads: int
        """
        self.size = size
        self.resolution = resolution
        self.data_source = data_source
        self.maxcc = maxcc
        self.time_difference = time_difference
        self.cache_folder = cache_folder
        self.max_threads = max_threads

        self.bands_feature = bands_feature
        self.bands = bands or data_source.bands() if bands_feature else []
        self.additional_data = additional_data or []

        self.all_bands = self.bands + [f_name for _, f_name in self.additional_data]

    @staticmethod
    def request_from_date(request, date, maxcc):
        ''' Make a deep copy of a request and sets it's (from, to) range according to the provided 'date' argument

        :param request: Path to cache_folder. If set to None (default) requests will not be cached.
        :type request: str
        '''
        date_from, date_to = date, date + dt.timedelta(seconds=1)
        time_from, time_to = date_from.isoformat() + 'Z', date_to.isoformat() + 'Z'

        request = deepcopy(request)
        for data in request['input']['data']:
            time_range = data['dataFilter']['timeRange']
            time_range['from'] = time_from
            time_range['to'] = time_to

            # this should be moved to sentinelhub-py package, it was done here to avoid doing another release of sh-py
            data['dataFilter']['maxCloudCoverage'] = int(maxcc * 100)

        return request

    def generate_evalscript(self):
        ''' Generate the evalscript to be passed with the request, based on chosen bands
        '''
        evalscript = """
            function setup() {{
                return {{
                    input: [{{
                        bands: {bands},
                        units: "DN"
                    }}],
                    output: {{
                        id:"default",
                        bands: {num_bands},
                        sampleType: SampleType.UINT16
                    }}
                }}
            }}

            function updateOutputMetadata(scenes, inputMetadata, outputMetadata) {{
                outputMetadata.userData = {{ "norm_factor":  inputMetadata.normalizationFactor }}
            }}

            function evaluatePixel(sample) {{
                return {samples}
            }}
        """

        samples = ', '.join(['sample.{}'.format(band) for band in self.all_bands])
        samples = '[{}]'.format(samples)

        return evalscript.format(bands=json.dumps(self.all_bands), num_bands=len(self.all_bands), samples=samples)

    def get_dates(self, bbox, time_interval):
        ''' Make a WebFeatureService request to get dates and clean them according to self.time_difference
        '''
        wfs = WebFeatureService(
            bbox=bbox, time_interval=time_interval, data_source=self.data_source, maxcc=self.maxcc
        )

        dates = wfs.get_dates()

        if len(dates) == 0:
            raise ValueError("No available images for requested time range: {}".format(time_interval))

        dates = sorted(dates)
        dates = [dates[0]] + [d2 for d1, d2 in zip(dates[:-1], dates[1:]) if d2 - d1 > self.time_difference]
        return dates

    @staticmethod
    def size_from_resolution(bbox, resolution):
        ''' Calculate size_x and size_y based on provided bbox and resolution
        '''
        bbox = list(bbox)
        size_x = int((bbox[2] - bbox[0]) / resolution)
        size_y = int((bbox[3] - bbox[1]) / resolution)
        return size_x, size_y

    def execute(self, eopatch=None, bbox=None, time_interval=None):
        ''' Make a WFS request to get valid dates, download an image for each valid date and store it in an EOPatch

        :param eopatch: input EOPatch
        :type eopatch: EOPatch
        '''

        if self.size is not None:
            size_x, size_y = self.size
        elif self.resolution is not None:
            size_x, size_y = self.size_from_resolution(bbox, self.resolution)

        responses = [shr.response('default', 'image/tiff'), shr.response('userdata', 'application/json')]
        request = shr.body(
            request_bounds=shr.bounds(crs=bbox.crs.opengis_string, bbox=list(bbox)),
            request_data=[shr.data(data_type=self.data_source.api_identifier())],
            request_output=shr.output(size_x=size_x, size_y=size_y, responses=responses),
            evalscript=self.generate_evalscript()
        )

        request_args = dict(
            url=SHConfig().get_sh_processing_api_url(),
            headers={"accept": "application/tar", 'content-type': 'application/json'},
            data_folder=self.cache_folder,
            hash_save=bool(self.cache_folder),
            request_type='POST',
            data_type=MimeType.TAR
        )

        dates = self.get_dates(bbox, time_interval)
        requests = (self.request_from_date(request, date, self.maxcc) for date in dates)
        requests = [DownloadRequest(post_values=payload, **request_args) for payload in requests]

        LOGGER.debug('Downloading %d requests of type %s', len(requests), str(self.data_source))
        LOGGER.debug('Downloading bands: [%s]', ', '.join(self.all_bands))
        client = SentinelHubDownloadClient()
        images = client.download(requests)
        LOGGER.debug('Downloads complete')

        images = ((img['default.tif'], img['userdata.json']) for img in images)
        images = [(img, meta.get('norm_factor', 0) if meta else 0) for img, meta in images]

        eopatch = EOPatch() if eopatch is None else eopatch

        eopatch.timestamp = dates
        eopatch.bbox = bbox

        shape = len(dates), size_y, size_x

        # exctract additional_data from the received images each as a separate feature
        for f_type, f_name in self.additional_data:
            idx = self.all_bands.index(f_name)
            feature_arrays = [np.atleast_3d(img)[..., idx] for img, norm_factor in images]
            eopatch[(f_type, f_name)] = np.asarray(feature_arrays).reshape(*shape, 1).astype(np.bool)

        # exctract bands from the received and save them as self.bands_feature
        if self.bands:
            img_bands = len(self.bands)
            img_arrays = [img[..., slice(img_bands)].astype(np.float32) * norm_factor for img, norm_factor in images]
            eopatch[self.bands_feature] = np.round(np.asarray(img_arrays).reshape(*shape, img_bands), 4)

        eopatch.meta_info['service_type'] = 'processing'
        eopatch.meta_info['size_x'] = size_x
        eopatch.meta_info['size_y'] = size_y
        eopatch.meta_info['maxcc'] = self.maxcc
        eopatch.meta_info['time_interval'] = time_interval
        eopatch.meta_info['time_difference'] = self.time_difference

        return eopatch