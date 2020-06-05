from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

from typing import Callable
from bs4 import BeautifulSoup

import codecs
import hashlib
import logging
import random
import time
import requests
import tempfile
import stem.process
from stem import Signal
from stem.control import Controller
from fake_useragent import UserAgent
from .publication import _SearchScholarIterator
from .author import Author
from .publication import Publication

_HEADERS = {
    'accept-language': 'en-US,en',
    'accept': 'text/html,application/xhtml+xml,application/xml'
}
_HOST = 'https://scholar.google.com{0}'

_PUBSEARCH = '"/scholar?hl=en&q={0}"'
_SCHOLARCITERE = r'gs_ocit\(event,\'([\w-]*)\''


class Singleton(type):
    _instances = {}

    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            cls._instances[cls] = super(Singleton, cls).__call__(*args,
                                                                 **kwargs)
        return cls._instances[cls]


class Navigator(object, metaclass=Singleton):
    """A class used to navigate pages on google scholar."""

    def __init__(self):
        super(Navigator, self).__init__()
        logging.basicConfig(filename='scholar.log', level=logging.INFO)
        self.logger = logging.getLogger('scholarly')
        self._proxy_gen = None
        
        # Setting requests timeout to be reasonably long
        # to accomodate slowness of the Tor network
        self._TIMEOUT = 10
        self._max_retries = 5

    def __del__(self):
        if self._tor_process:
            self._tor_process.kill()

    def _get_page(self, pagerequest: str) -> str:
        """Return the data from a webpage

        :param pagerequest: the page url
        :type pagerequest: str
        :returns: the text from a webpage
        :rtype: {str}
        :raises: Exception
        """
        self.logger.info("Getting %s", pagerequest)
        # Space a bit the requests to avoid overloading the servers
        time.sleep(random.uniform(1,5))
        resp = None
        tries = 0
        while tries < self._max_retries:
            # If proxy/Tor was setup, use it.
            # Otherwise the local IP is used
            session = requests.Session()
            if self._proxy_works:
                session.proxies = self.proxies

            try:
                _HEADERS['User-Agent'] = UserAgent().random
                _GOOGLEID = hashlib.md5(str(random.random()).encode('utf-8')).hexdigest()[:16]
                _COOKIES = {'GSP': 'ID={0}:CF=4'.format(_GOOGLEID)}

                resp = session.get(pagerequest,
                                   headers=_HEADERS,
                                   cookies=_COOKIES,
                                   timeout=self._TIMEOUT)

                if resp.status_code == 200:
                    if not self._has_captcha(resp.text):
                        return resp.text
                    self.logger.info("Got a CAPTCHA. Retrying.")
                else:
                    self.logger.info(f"""Response code {resp.status_code}.
                                    Retrying...""")

            except Exception as e:
                err = f"Exception {e} while fetching page. Retrying."
                self.logger.info(err)
            finally:
                session.close()

            # If we reached this point, an error has happened.
            tries += 1
            
            # If we have a proxy generator setup, we get the next proxy
            if self._proxy_gen:
                self.logger.info(f"Try #{tries} failed. Switching proxy.")
                # Try to get another proxy
                new_proxy = self._proxy_gen()
                while (not self._use_proxy(new_proxy)):
                    new_proxy = self._proxy_gen()
            else:
                # we only increase the tries when we cannot refresh id
                # to avoid an infinite loop
                tries += 1
        raise Exception("Cannot fetch the page from Google Scholar.")

    def _set_retries(self, num_retries: int) -> None:
        if (num_retries < 0):
            raise ValueError("num_retries must not be negative")
        self._max_retries = num_retries




    def _has_captcha(self, text: str) -> bool:
        """Tests whether an error or captcha was shown.

        :param text: the webpage text
        :type text: str
        :returns: whether or not an error occurred
        :rtype: {bool}
        """
        flags = ["Please show you're not a robot",
                 "network may be sending automated queries",
                 "have detected unusual traffic from your computer",
                 "scholarly_captcha",
                 "/sorry/image",
                 "enable JavaScript"]
        return any([i in text for i in flags])

    def _get_soup(self, url: str) -> BeautifulSoup:
        """Return the BeautifulSoup for a page on scholar.google.com"""
        html = self._get_page(_HOST.format(url))
        html = html.replace(u'\xa0', u' ')
        res = BeautifulSoup(html, 'html.parser')
        try:
            self.publib = res.find('div', id='gs_res_glb').get('data-sva')
        except Exception:
            pass
        return res

    def search_authors(self, url: str):
        """Generator that returns Author objects from the author search page"""
        soup = self._get_soup(url)

        while True:
            rows = soup.find_all('div', 'gsc_1usr')
            self.logger.info("Found %d authors", len(rows))
            for row in rows:
                yield Author(self, row)
            cls1 = 'gs_btnPR gs_in_ib gs_btn_half '
            cls2 = 'gs_btn_lsb gs_btn_srt gsc_pgn_pnx'
            next_button = soup.find(class_=cls1+cls2)  # Can be improved
            if next_button and 'disabled' not in next_button.attrs:
                self.logger.info("Loading next page of authors")
                url = next_button['onclick'][17:-1]
                url = codecs.getdecoder("unicode_escape")(url)[0]
                soup = self._get_soup(url)
            else:
                self.logger.info("No more author pages")
                break

    def search_publication(self, url: str,
                           filled: bool = False) -> Publication:
        """Search by scholar query and return a single Publication object

        :param url: the url to be searched at
        :type url: str
        :param filled: Whether publication should be filled, defaults to False
        :type filled: bool, optional
        :returns: a publication object
        :rtype: {Publication}
        """
        soup = self._get_soup(url)
        res = Publication(self, soup.find_all('div', 'gs_or')[0], 'scholar')
        if filled:
            res.fill()
        return res

    def search_publications(self, url: str) -> _SearchScholarIterator:
        """Returns a Publication Generator given a url

        :param url: the url where publications can be found.
        :type url: str
        :returns: An iterator of Publications
        :rtype: {_SearchScholarIterator}
        """
        return _SearchScholarIterator(self, url)
