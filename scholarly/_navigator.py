from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

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

from selenium import webdriver
from selenium.webdriver.common.proxy import Proxy, ProxyType
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import TimeoutException
from datetime import datetime
from fake_useragent import UserAgent

_GOOGLEID = hashlib.md5(str(random.random()).encode('utf-8')).hexdigest()[:16]
_COOKIES = {'GSP': 'ID={0}:CF=4'.format(_GOOGLEID)}
_HEADERS = {
    'accept-language': 'en-US,en',
    'accept': 'text/html,application/xhtml+xml,application/xml'
}
_HOST = 'https://scholar.google.com{0}'

_PUBSEARCH = '"/scholar?hl=en&q={0}"'
_SCHOLARCITERE = r'gs_ocit\(event,\'([\w-]*)\''
_CAPTCHA = "iframe[name^='a-'][src^='https://www.google.com/recaptcha/api2/anchor?']"


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
        # If we use a proxy or Tor, we set this to True
        self._proxy_works = False
        # If we have a Tor server that we can refresh, we set this to True
        self._tor_process = None
        self._can_refresh_tor = True
        self._tor_control_port = None
        self._tor_password = None
        # Setting requests timeout to be reasonably long
        # to accomodate slowness of the Tor network
        self._TIMEOUT = 10
        self._MAX_RETRIES = 5
        self.session = None

    def __exit__(self):
        self._session_close()

    def __del__(self):
        if self._tor_process:
            self._tor_process.kill()

        self._session_close()

    def _get_new_chrome_session(self,
                                use_proxy: bool = True) -> webdriver.Chrome:
        """Creates a Chrome based agent

        The agent receives a randomized window and agent.
        Optimized to minimized detection by the scraped server
        :param use_proxy: whether or not to use proxy, defaults to True
        :type use_proxy: bool, optional
        :returns: a chrome based webdriver
        :rtype: {webdriver.Chrome}
        """
        chrome_options = webdriver.ChromeOptions()

        if self._proxy_works:
            chrome_options.add_argument(
                '--proxy-server={}'.format(self.proxies['http']))

        chrome_options.add_argument(f'user-agent={UserAgent().random}')
        chrome_options.add_experimental_option(
            "excludeSwitches",
            ["enable-automation"])
        chrome_options.add_experimental_option('useAutomationExtension', False)

        driver = webdriver.Chrome(options=chrome_options)
        driver.set_window_size(
            random.randint(100, 1025),
            random.randint(100, 1025))
        return driver

    def _get_new_firefox_session(self,
                                 use_proxy: bool = True) -> webdriver.Firefox:
        """Creates a Firefox based agent

        The agent receives a randomized window and agent.
        Optimized to minimized detection by the scraped server

        Keyword Arguments:
            use_proxy {bool} -- whether or not to use proxy (default: {True})

        Returns:
            webdriver.Firefox -- a chrome based webdriver
        """

        profile = webdriver.FirefoxProfile()
        profile.set_preference("dom.webdriver.enabled", False)
        profile.set_preference('useAutomationExtension', False)
        profile.set_preference(
            "general.useragent.override", UserAgent().random)
        profile.update_preferences()

        if use_proxy:
            proxy = Proxy({
                "proxyType": ProxyType.MANUAL,
                "httpProxy": self.proxies['http'],
                "httpsProxy": self.proxies['https'],
                "socksProxy": self.proxies['https'],
                "sslProxy": self.proxies['https'],
                "ftpProxy": self.proxies['https'],
                "noProxy": ""
            })
            return webdriver.Firefox(firefox_profile=profile, proxy=proxy)
        else:
            return webdriver.Firefox(firefox_profile=profile)

    def _session_close(self):
        if self.session is not None:
            if self._use_selenium:
                self.session.quit()
            else:
                self.session.close()

    def _session_refresher(self):
        self._session_close()

        if self._can_refresh_tor:
            self._refresh_tor_id(self._tor_control_port, self._tor_password)

        return self._get_new_session()

    def _get_new_session(self):
        try:
            res = self._get_new_chrome_session()
            self._use_selenium = True
            return res
        except Exception as e:
            self.logger.info(f"{e}\nCould not use Chrome, trying Firefox.")

        try:
            res = self._get_new_firefox_session()
            self._use_selenium = True
            return res
        except Exception as e:
            self.logger.info(f"{e}\nCould not use Firefox, trying Requests.")

        try:
            res = requests.Session()
            if self._proxy_works:
                res.proxies = self.proxies

            self._use_selenium = False
        except Exception as e:
            self.logger.info(f"{e}\nCould not use anything, aborting.")
            raise Exception(e)

        return res

    def _get_page_selenium(self, pagerequest: str) -> str:

        # just in case we get a good TOR server we wait to not overload it
        time.sleep(random.uniform(2, 5))
        searching = True
        # Tries to retrieve the page until no captcha is shown
        while searching:
            try:
                self.session.get(pagerequest)
                # waiting page to load
                wait = WebDriverWait(self.session, 100)
                #wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, _CAPTCHA)))
                
                # Waits for 5 minutes until user passes the captcha
                # If no captcha appears then there will be no block
                wait = WebDriverWait(self.session, 300)
                wait.until_not(EC.presence_of_element_located((By.CSS_SELECTOR, _CAPTCHA)))


                text = self.session.page_source
                if self._has_captcha(text) or self._has_error(text):
                    self._refresh_tor_id(
                        self._tor_control_port, self._tor_password)
                    self.session = self._get_new_session()
                else:
                    searching = False
            except TimeoutException:
                raise Exception("Server is too slow, stopping search")

        return self.session.page_source

    def _get_page_requests(self, pagerequest: str) -> str:
        resp = None
        tries = 0
        while tries < self._MAX_RETRIES:
            try:
                _HEADERS['User-Agent'] = UserAgent().random

                resp = self.session.get(pagerequest,
                                        headers=_HEADERS,
                                        cookies=_COOKIES,
                                        timeout=self._TIMEOUT)

                if resp.status_code == 200:
                    if (self._has_captcha(resp.text) or
                            self._has_error(resp.text)):
                        raise Exception("Got a CAPTCHA or ERROR. Retrying.")
                    else:
                        self.session.close()
                        return resp.text
                else:
                    self.logger.info(f"""Response code {resp.status_code}.
                                    Retrying...""")
                    raise Exception(f"Status code {resp.status_code}")

            except Exception as e:
                err = f"Exception {e} while fetching page. Retrying."
                err = f"Please consider using Selenium!!!"
                self.logger.info(err)
                # Check if Tor is running and refresh it
                self.logger.info("Refreshing Tor ID...")
                self.session.close()
                if self._can_refresh_tor:
                    self._refresh_tor_id(
                        self._tor_control_port, self._tor_password)
                    time.sleep(5)  # wait for the refresh to happen
                # Increase tries since user is not using selenium to solve
                # the captchas. This preserves the Tor network and avoid
                # overloading.
                tries += 1

    def _get_page(self, pagerequest: str) -> str:
        """Return the data from a webpage

        :param pagerequest: the page url
        :type pagerequest: str
        :returns: the text from a webpage
        :rtype: {str}
        :raises: Exception
        """
        protocol = pagerequest[:4]
        assert protocol == "http", f"Invalid url '{pagerequest}', \
user http or https. Aborting."

        self.logger.info("Getting %s", pagerequest)
        # Space a bit the requests to avoid overloading the servers
        try:
            if self.session is None:
                self.session = self._get_new_session()

            if self._use_selenium:
                return self._get_page_selenium(pagerequest)
            else:
                return self._get_page_requests(pagerequest)
        except Exception:
            raise Exception("Cannot fetch the page from Google Scholar.")

    def _check_proxy(self, proxies) -> bool:
        """Checks if a proxy is working.
        :param proxies: A dictionary {'http': url1, 'https': url1}
                        with the urls of the proxies
        :returns: whether the proxy is working or not
        :rtype: {bool}
        """
        with requests.Session() as session:
            session.proxies = proxies
            try:
                # Changed to twitter so we dont ping google twice every time
                resp = session.get("http://www.twitter.com",
                                   timeout=self._TIMEOUT)
                self.logger.info("Proxy Works!")
                return resp.status_code == 200
            except Exception as e:
                self.logger.info(f"Proxy not working: Exception {e}")
                return False

    def _refresh_tor_id(self, tor_control_port: int, password: str) -> bool:
        """Refreshes the id by using a new ToR node.

        :returns: Whether or not the refresh was succesful
        :rtype: {bool}
        """
        try:
            with Controller.from_port(port=tor_control_port) as controller:
                if password:
                    controller.authenticate(password=password)
                else:
                    controller.authenticate()
                controller.signal(Signal.NEWNYM)
            return True
        except Exception as e:
            err = f"Exception {e} while refreshing TOR. Retrying..."
            self.logger.info(err)
            return False

    def _use_proxy(self, http: str, https: str) -> bool:
        """Allows user to set their own proxy for the connection session.
        Sets the proxy, and checks if it woks,

        :param http: the http proxy
        :type http: str
        :param https: the https proxy
        :type https: str
        :returns: if the proxy works
        :rtype: {bool}
        """
        self.logger.info("Enabling proxies: http=%r https=%r", http, https)

        proxies = {'http': http, 'https': https}
        self._proxy_works = self._check_proxy(proxies)
        if self._proxy_works:
            self.proxies = proxies
        else:
            self.proxies = {'http': None, 'https': None}

        return self._proxy_works

    def _setup_tor(self,
                   tor_sock_port: int,
                   tor_control_port: int,
                   tor_password: str):
        """
        Setting up Tor Proxy

        :param tor_sock_port: the port where the Tor sock proxy is running
        :type tor_sock_port: int
        :param tor_control_port: the port where the Tor control server is running
        :type tor_control_port: int
        :param tor_password: the password for the Tor control server
        :type tor_password: str
        """

        proxy = f"socks5://127.0.0.1:{tor_sock_port}"
        self._use_proxy(http=proxy, https=proxy)

        # self._can_refresh_tor = self._refresh_tor_id(tor_control_port, tor_password)
        if self._can_refresh_tor:
            self._tor_control_port = tor_control_port
            self._tor_password = tor_password
        else:
            self._tor_control_port = None
            self._tor_password = None

        return {
            "proxy_works": self._proxy_works,
            "refresh_works": self._can_refresh_tor,
            "proxies": self.proxies,
            "tor_control_port": tor_control_port,
            "tor_sock_port": tor_sock_port
        }

    def _launch_tor(self,
                    tor_cmd=None,
                    tor_sock_port=None,
                    tor_control_port=None):
        '''
        Starts a Tor client running in a schoar-specific port,
        together with a scholar-specific control port.
        '''
        self.logger.info("Attempting to start owned Tor as the proxy")

        if tor_cmd is None:
            self.logger.info(
                "No tor_cmd argument passed. \
                This should point to the location of tor executable")
            return {
                "proxy_works": False,
                "refresh_works": False,
                "proxies": {'http': None, 'https': None},
                "tor_control_port": None,
                "tor_sock_port": None
            }

        if tor_sock_port is None:
            # Picking a random port to avoid conflicts
            # with simultaneous runs of scholarly
            tor_sock_port = random.randrange(9000, 9500)

        if tor_control_port is None:
            # Picking a random port to avoid conflicts
            # with simultaneous runs of scholarly
            tor_control_port = random.randrange(9500, 9999)

        # TODO: Check that the launched Tor process stops after scholar is done
        self._tor_process = stem.process.launch_tor_with_config(
            tor_cmd=tor_cmd,
            config={
                'ControlPort': str(tor_control_port),
                'SocksPort': str(tor_sock_port),
                'DataDirectory': tempfile.mkdtemp()
                # TODO Perhaps we want to also set a password here
            },
        )
        return self._setup_tor(tor_sock_port,
                               tor_control_port,
                               tor_password=None)

    def _has_error(self, text: str):
        """Tests whether an error was shown.

        :param text: the webpage text
        :type text: str
        :returns: whether or not an error occurred
        :rtype: {bool}
        """
        flags = ["network may be sending automated queries",
                 "have detected unusual traffic from your computer",
                 "/sorry/image",
                 "enable JavaScript"]
        return any([i in text for i in flags])

    def _has_captcha(self, text: str) -> bool:
        """Tests whether a captcha was shown.

        :param text: the webpage text
        :type text: str
        :returns: whether or not an error occurred
        :rtype: {bool}
        """
        flags = ["Please show you're not a robot",
                 "have detected unusual traffic from your computer",
                 "scholarly_captcha"]
        return any([i in text for i in flags])

    def _get_soup(self, url: str, use_host: bool = True) -> BeautifulSoup:
        """Return the BeautifulSoup for a page on scholar.google.com"""
        if use_host:
            url = _HOST.format(url)
        html = self._get_page(url)
        html = html.replace(u'\xa0', u' ')
        res = BeautifulSoup(html, 'html.parser')
        try:
            self.publib = res.find('div', id='gs_res_glb').get('data-sva')
        except Exception:
            pass
        return res

    def _search_authors(self, url: str):
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

    def _search_publication(self, url: str,
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

    def _search_publications(self, url: str) -> _SearchScholarIterator:
        """Returns a Publication Generator given a url

        :param url: the url where publications can be found.
        :type url: str
        :returns: An iterator of Publications
        :rtype: {_SearchScholarIterator}
        """
        return _SearchScholarIterator(self, _HOST.format(url))
