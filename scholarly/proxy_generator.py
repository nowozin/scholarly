from fp.fp import FreeProxy
from typing import Callable
import requests

class ProxyGenerator(object):
    
    def __init__(self, 
                 launch_tor: bool=False, tor_cmd: str=None, tor_sock_port: str=None, tor_control_port: str=None,
                 use_tor: bool = False, tor_password: str=None,
                 use_freeproxy: bool=False,
                 use_proxy: bool=False, http_proxy: str=None, https_proxy: str=None):
        
        check = (launch_tor ^ use_tor ^ use_freeproxy ^ use_proxy == 1)
        assert check, "One and only one of launch_tor, use_tor, use_freeproxy, use_proxy can be True"
        
        self.launch_tor = launch_tor
        self.use_tor = use_tor
        self.use_freeproxy = use_freeproxy
        self.use_proxy = use_proxy

        self._tor_process = None
        self._tor_control_port = None
        self._tor_sock_port = None
        self._tor_password = None
        
        if self.launch_tor:
            self._launch_tor(tor_cmd, tor_sock_port, tor_control_port)
            
        if self.use_tor:
            self._setup_tor(tor_sock_port, tor_control_port, tor_password)            
        
                        "http_proxy": self.http_proxy,
                "https_proxy": self.https_proxy,


        

    def __del__(self):
        if self._tor_process:
            self._tor_process.kill()        
        
    def check_proxy(http: str, https: str = None):
        """Checks in the proxy works and returns a True/False value respectively. 
        It also returns the exit IP address if the proxy works.

        :param http: the http proxy
        :type http: str
        :param https: the https proxy (default to the same as http)
        :type https: str
        :returns: if the proxy works and the IP address that it is using
        """

        with requests.Session() as session:
            proxies = {'http': http, 'https': https}
            try:
                # Changed to twitter so we dont ping google twice every time
                resp = session.get("http://httpbin.org/ip", timeout=self._TIMEOUT)
                if resp.status_code == 200:
                    ip_addr = resp.json()['origin']
                    self.logger.info(f"Proxy works. IP: {ip_addr}")
                    return {
                        "proxy_works": True,
                        "ip_addr": ip_addr
                    }
            except Exception as e:
                self.logger.info(f"Exception while testing proxy: {e}")

        return {
            "proxy_works": False,
            "ip_addr": None
        }        
        
        
    def get_next_proxy(self):
        
        if self.launch_tor or self.use_tor:
            return self._get_next_tor_exit()
                    
        if self.use_freeproxy:
            return self._get_next_freeproxy()

        if self.use_proxy:
            # Not a renewable proxy. We just check that it works, and return
            # the exit IP address
            result = self.check_proxy(http=self.http_proxy, https=self.https_proxy)
            return {
                "http_proxy": self.http_proxy,
                "https_proxy": self.https_proxy,
                "proxy_works": result["proxy_works"],
                "ip_addr": result["ip_addr"]
            }     
    
    def _get_next_freeproxy(self, max_retries=100):
        '''
        Uses the FreeProxy library and fetches a new proxy. We check that 
        the proxy works before returning it.
        '''
        for _ in range(max_retries):
            proxy = FreeProxy(rand=True, timeout=1).get()
            result = self.check_proxy(http=proxy, https=proxy)
            if result['proxy_works']:
                break
        return {
            "proxy": proxy,
            "proxy_works": result["proxy_works"],
            "ip_addr": result["ip_addr"]
        }
            
    def _get_next_tor_exit(self, max_retries=100):
        '''
        Use the control port of Tor, and asks for a new exit node 
        from the Tor network.
        
        TODO: Structure is very similar with _get_next_freeproxy. Consider merging
        '''        
        for _ in range(max_retries):
            success = self._refresh_tor_id(self._tor_control_port, self._tor_password)
            if not success: continue 
                
            proxy = f"socks5://127.0.0.1:{tor_sock_port}"
            result = self.check_proxy(http=proxy, https=proxy)
            if result['proxy_works']:
                break
            
        return {
            "proxy": proxy,
            "proxy_works": result["proxy_works"],
            "ip_addr": result["ip_addr"]
        }

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

    def _launch_tor(self, tor_cmd=None, tor_sock_port=None, tor_control_port=None):
        '''
        Starts a Tor client running in a schoar-specific port,
        together with a scholar-specific control port.
        '''
        self.logger.info("Attempting to start owned Tor as the proxy")

        if tor_cmd is None:
            self.logger.info("No tor_cmd argument passed. This should point to the location of tor executable")
            return {
                "proxy_works": False,
                "refresh_works": False,
                "http_proxy": None,
                
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

        if self._tor_process:
            self._tor_process.kill()             
        
        self._tor_process = stem.process.launch_tor_with_config(
            tor_cmd=tor_cmd,
            config={
                'ControlPort': str(tor_control_port),
                'SocksPort': str(tor_sock_port),
                'DataDirectory': tempfile.mkdtemp()
                # TODO Perhaps we want to also set a password here
            },
            # take_ownership=True # Taking this out for now, as it seems to cause trouble
        )
        return self._setup_tor(tor_sock_port, tor_control_port, tor_password=None)
        
        

    def _setup_tor(self, tor_sock_port: int, tor_control_port: int, tor_password: str):
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
        \

        self._can_refresh_tor = self._refresh_tor_id(tor_control_port, tor_password)
        if self._can_refresh_tor:
            self._tor_control_port = tor_control_port
            self._tor_password = tor_password
            self._tor_sock_port = tor_sock_port
        else:
            self._tor_control_port = None
            self._tor_password = None
            self._tor_sock_port = None

        return {
            "proxy_works": self._proxy_works,
            "refresh_works": self._can_refresh_tor,
            "proxies": self.proxies,
            "tor_control_port": tor_control_port,
            "tor_sock_port": tor_sock_port
        }
        
    def _set_proxy_generator(self, gen: Callable[..., str]) -> bool:
        self._proxy_gen = gen
        return True        