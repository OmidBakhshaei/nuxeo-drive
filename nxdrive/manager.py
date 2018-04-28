# coding: utf-8
import os
import platform
import sip
import subprocess
import sys
import unicodedata
import urllib2
import uuid
from collections import namedtuple
from logging import getLogger
from urlparse import urlparse

import pypac
from PyQt4 import QtCore
from PyQt4.QtGui import QApplication
from PyQt4.QtScript import QScriptEngine
from PyQt4.QtWebKit import qWebKitVersion

from . import __version__
from .client import LocalClient
from .client.base_automation_client import get_proxies_for_handler
from .logging_config import FILE_HANDLER
from .notification import DefaultNotificationService
from .options import Options, server_updater
from .osi import AbstractOSIntegration
from .updater import updater
from .utils import ENCODING, decrypt, encrypt, force_decode, normalized_path

if AbstractOSIntegration.is_windows():
    import _winreg
    import win32api
    import win32clipboard
elif AbstractOSIntegration.is_mac():
    import SystemConfiguration

log = getLogger(__name__)


class FolderAlreadyUsed(Exception):
    pass


class EngineTypeMissing(Exception):
    pass


class MissingToken(Exception):
    pass


class ProxySettings(object):
    """ Summarize HTTP proxy settings. """

    def __init__(self, dao=None, config='System', proxy_type=None, server=None,
                 port=None, authenticated=False, username=None, password=None,
                 exceptions=None):
        self.config = config
        self.proxy_type = proxy_type
        self.server = server
        self.port = port
        self.authenticated = authenticated
        self.username = username
        self.password = password
        self.exceptions = exceptions
        self.pac_url = None
        if dao is not None:
            self.load(dao)

    def from_url(self, url):
        if '://' not in url:
            url = 'all://' + url
        url_obj = urlparse(url)
        self.username = url_obj.username
        self.password = url_obj.password
        self.authenticated = (self.username is not None and
                              self.password is not None)
        self.port = url_obj.port
        self.server = url_obj.hostname
        if url_obj.scheme == "all":
            self.proxy_type = None
        else:
            self.proxy_type = url_obj.scheme
        self.config = "Manual"

    def to_url(self, with_credentials=True):
        if self.config != "Manual":
            return ""
        result = ""
        if self.proxy_type is not None:
            result = self.proxy_type + "://"
        if with_credentials and self.authenticated:
            result += self.username + ":" + self.password + "@"
        if self.server is not None:
            if self.port is None:
                result += self.server
            else:
                result += self.server + ":" + str(self.port)
        return result

    def set_exceptions(self, exceptions):
        self.exceptions = exceptions

    def save(self, dao, token=None):
        # Encrypt password with token as the secret
        if token is None:
            token = dao.get_config("device_id")
        if token is None:
            raise MissingToken('Your token has been revoked, please update'
                               ' your password to acquire a new one.')
        token += '_proxy'
        password = encrypt(self.password, token)
        dao.update_config("proxy_password", password)
        dao.update_config("proxy_username", self.username)
        dao.update_config("proxy_exceptions", self.exceptions)
        dao.update_config("proxy_port", self.port)
        dao.update_config("proxy_type", self.proxy_type)
        dao.update_config("proxy_config", self.config)
        dao.update_config("proxy_server", self.server)
        dao.update_config("proxy_authenticated", self.authenticated)
        dao.update_config("proxy_pac_url", self.pac_url)

    def load(self, dao, token=None):
        self.config = dao.get_config("proxy_config", "System")
        self.proxy_type = dao.get_config("proxy_type")
        self.port = dao.get_config("proxy_port")
        self.server = dao.get_config("proxy_server")
        self.username = dao.get_config("proxy_username")
        password = dao.get_config("proxy_password")
        self.exceptions = dao.get_config("proxy_exceptions")
        self.authenticated = (dao.get_config("proxy_authenticated", "0") == "1")
        self.pac_url = dao.get_config("proxy_pac_url")
        if token is None:
            token = dao.get_config("device_id")
        if password is not None and token is not None:
            token += '_proxy'
            self.password = decrypt(password, token)
        else:
            # If no server binding or no token available
            # (possibly after token revocation) reset password
            self.password = ''

    def __repr__(self):
        return ("ProxySettings<config=%s, proxy_type=%s, server=%s, port=%s, "
                "authenticated=%r, username=%s, exceptions=%s>") % (
                    self.config, self.proxy_type, self.server, self.port,
                    self.authenticated, self.username, self.exceptions)

    @staticmethod
    def validate_proxy(proxy_server, target_url):
        """
        Validate a proxy server for the give the target URL.

        :param proxy_server: The proxy server to validate
        :param target_url: The target URL to check
        :return: (True, proxy_setting) if proxy server is valid for the
                                       target_url
                 (False, proxy_setting) if proxy server does not work for
                                        the target_url
        """

        proxy_setting = {}
        log.trace('Validating proxy server: %s for %s', proxy_server,
                  target_url)
        try:
            if proxy_server.upper() == 'DIRECT':
                opener = urllib2.build_opener()
            else:
                proxy_setting = {'http': proxy_server, 'https': proxy_server}
                proxy = urllib2.ProxyHandler(proxy_setting)
                opener = urllib2.build_opener(proxy)
            response = opener.open(target_url)
            if response:
                response.read()
            response.close()
            return True, proxy_setting
        except urllib2.HTTPError:
            log.exception('Invalid proxy server')
        return False, proxy_setting

    def get_proxies_automatic(self, url):
        """
        Get the proxy for the given URL.

        :param url: The URL for which we need to find the proxy server
        :return: proxy_setting using first valid proxy server IP (for URL),
                 in a list of available proxy servers.
                 Empty dictionary if there is no proxy server available
                for given URL.
        """

        default = None
        if self.config != 'Automatic':
            return default

        try:
            response = urllib2.urlopen(self.pac_url)
        except urllib2.URLError:
            log.exception('Network error')
            return default
        else:
            pac_script = response.read()
            response.close()

        try:
            pac_data = pypac.parser.PACFile(pac_script)
            resolver = pypac.resolver.ProxyResolver(pac_data)
        except (pypac.parser.MalformedPacError,
                pypac.resolver.ProxyConfigExhaustedError):
            log.exception('PAC error')
            return default
        else:
            proxy_list = resolver.get_proxies(url)

        for item in proxy_list:
            proxy_server = item.lstrip('PROXY ')
            working, proxy_setting = self.validate_proxy(
                proxy_server=proxy_server, target_url=url)
            if working:
                return proxy_setting

        return default


class Manager(QtCore.QObject):
    proxyUpdated = QtCore.pyqtSignal(object)
    clientUpdated = QtCore.pyqtSignal(object, object)
    engineNotFound = QtCore.pyqtSignal(object)
    newEngine = QtCore.pyqtSignal(object)
    dropEngine = QtCore.pyqtSignal(object)
    initEngine = QtCore.pyqtSignal(object)
    aboutToStart = QtCore.pyqtSignal(object)
    started = QtCore.pyqtSignal()
    stopped = QtCore.pyqtSignal()
    suspended = QtCore.pyqtSignal()
    resumed = QtCore.pyqtSignal()
    _singleton = None
    app_name = 'Nuxeo Drive'

    __device_id = None

    @staticmethod
    def get():
        return Manager._singleton

    def __init__(self):
        if Manager._singleton:
            raise RuntimeError('Only one instance of Manager can be created.')

        super(Manager, self).__init__()
        Manager._singleton = self

        # Primary attributes to allow initializing the notification center early
        self.nxdrive_home = os.path.realpath(
            os.path.expanduser(Options.nxdrive_home))
        if not os.path.exists(self.nxdrive_home):
            os.mkdir(self.nxdrive_home)
        self._create_dao()

        self.notification_service = DefaultNotificationService(self)
        self.osi = AbstractOSIntegration.get(self)

        if not Options.consider_ssl_errors:
            self._bypass_https_verification()

        self.direct_edit_folder = os.path.join(
            normalized_path(self.nxdrive_home),
            'edit')

        self._engine_definitions = None

        from .engine.engine import Engine
        from .engine.next.engine_next import EngineNext
        self._engine_types = {'NXDRIVE': Engine, 'NXDRIVENEXT': EngineNext}
        self._engines = None
        self.proxies = dict()
        self.proxy_exceptions = None
        self.updater = None
        self.server_config_updater = None
        if Options.proxy_server is not None:
            proxy = ProxySettings()
            proxy.from_url(Options.proxy_server)
            proxy.save(self._dao)

        # Set the logs levels option
        if FILE_HANDLER:
            FILE_HANDLER.setLevel(Options.log_level_file)

        # Force language
        if Options.force_locale is not None:
            self.set_config('locale', Options.force_locale)

        # Persist beta channel check
        Options.set('beta_channel', self.get_beta_channel(), setter='manual')

        # Keep a trace of installed versions
        if not self.get_config('original_version'):
            self.set_config('original_version', self.version)

        # Store the old version to be able to show release notes
        self.old_version = self.get_config('client_version')
        if self.old_version != self.version:
            self.set_config('client_version', self.version)
            self.clientUpdated.emit(self.old_version, self.version)

        # Add auto-lock on edit
        res = self._dao.get_config('direct_edit_auto_lock')
        if res is None:
            self._dao.update_config('direct_edit_auto_lock', '1')

        self.refresh_proxies()

        # Create DirectEdit
        self._create_autolock_service()
        self._create_direct_edit(Options.protocol_url)

        # Create notification service
        self._script_engine = None
        self._script_object = None
        self._started = False

        # Pause if in debug
        self._pause = Options.debug

        # Connect all Qt signals
        self.notification_service.init_signals()
        self.load()

        # Create the server's configuration getter verification thread
        self._create_server_config_updater()

        # Create the application update verification thread
        self.updater = self._create_updater()

        # Setup analytics tracker
        self._tracker = self._create_tracker()

        # Create the FinderSync listener thread
        if sys.platform == 'darwin':
            self._create_findersync_listener()

    @staticmethod
    def _bypass_https_verification():
        """
        Let's bypass HTTPS verification since many servers
        unfortunately have invalid certificates.
        See https://www.python.org/dev/peps/pep-0476/ and NXDRIVE-506.
        """

        import ssl

        log.warning('--consider-ssl-errors option is False, '
                    'will not verify HTTPS certificates')
        try:
            _context = ssl._create_unverified_context
        except AttributeError:
            log.info('Legacy Python that does not verify HTTPS certificates')
        else:
            log.info('Handle target environment that does not support HTTPS '
                     'verification: globally disable verification by '
                     'monkeypatching the ssl module though highly discouraged')
            ssl._create_default_https_context = _context

    def get_metrics(self):
        return {
            'version': self.version,
            'auto_start': self.get_auto_start(),
            'auto_update': self.get_auto_update(),
            'beta_channel': self.get_beta_channel(),
            'device_id': self.device_id,
            'tracker_id': self.get_tracker_id(),
            'tracking': self.get_tracking(),
            'sip_version': sip.SIP_VERSION_STR,
            'qt_version': QtCore.QT_VERSION_STR,
            'webkit_version': str(qWebKitVersion()),
            'pyqt_version': QtCore.PYQT_VERSION_STR,
            'python_version': platform.python_version(),
            'platform': platform.system(),
            'appname': self.app_name,
        }

    def open_help(self):
        self.open_local_file('https://doc.nuxeo.com/nxdoc/nuxeo-drive/')

    def _handle_os(self):
        # Be sure to register os
        self.osi.register_protocol_handlers()
        if self.get_auto_start():
            self.osi.register_startup()

    def _create_autolock_service(self):
        from .autolocker import ProcessAutoLockerWorker
        self.autolock_service = ProcessAutoLockerWorker(
            30, self._dao, folder=self.direct_edit_folder)
        self.started.connect(self.autolock_service._thread.start)
        return self.autolock_service

    def _create_tracker(self):
        if not self.get_tracking():
            return None

        from .engine.tracker import Tracker
        tracker = Tracker(self)
        # Start the tracker when we launch
        self.started.connect(tracker._thread.start)
        return tracker

    def _get_db(self):
        return os.path.join(normalized_path(self.nxdrive_home), 'manager.db')

    def get_dao(self):  # TODO: Remove
        return self._dao

    def _create_dao(self):
        from .engine.dao.sqlite import ManagerDAO
        self._dao = ManagerDAO(self._get_db())

    def _create_server_config_updater(self):
        # type: () -> None
        if not Options.update_check_delay:
            return

        self.server_config_updater = server_updater(self)
        self.started.connect(self.server_config_updater._thread.start)

    def _create_updater(self):
        updater_ = updater(self)
        self.started.connect(updater_._thread.start)
        return updater_

    def _create_findersync_listener(self):
        from .osi.darwin.darwin import FinderSyncListener
        self._findersync_listener = FinderSyncListener(self)
        self.started.connect(self._findersync_listener._thread.start)
        return self._findersync_listener

    def refresh_update_status(self):
        if self.updater:
            self.updater.refresh_status()

    def _create_direct_edit(self, url):
        from .direct_edit import DirectEdit
        self.direct_edit = DirectEdit(self, self.direct_edit_folder, url)
        self.started.connect(self.direct_edit._thread.start)
        return self.direct_edit

    def is_paused(self):  # TODO: Remove
        return self._pause

    def resume(self, euid=None):
        if not self._pause:
            return
        self._pause = False
        for uid, engine in self._engines.items():
            if euid is not None and euid != uid:
                continue
            log.debug('Resume engine %s', uid)
            engine.resume()
        self.resumed.emit()

    def suspend(self, euid=None):
        if self._pause:
            return
        self._pause = True
        for uid, engine in self._engines.items():
            if euid is not None and euid != uid:
                continue
            log.debug('Suspend engine %s', uid)
            engine.suspend()
        self.suspended.emit()

    def stop(self, euid=None):
        for uid, engine in self._engines.items():
            if euid is not None and euid != uid:
                continue
            if engine.is_started():
                log.debug('Stop engine %s', uid)
                engine.stop()
        if AbstractOSIntegration.is_mac():
            self.osi._cleanup()
        self.stopped.emit()

    def start(self, euid=None):
        self._started = True
        for uid, engine in self._engines.items():
            if euid is not None and euid != uid:
                continue
            if not self._pause:
                self.aboutToStart.emit(engine)
                log.debug('Launch engine %s', uid)
                try:
                    engine.start()
                except:
                    log.exception('Could not start the engine %s', uid)

        # Check only if manager is started
        self._handle_os()
        self.started.emit()

    def load(self):
        if self._engine_definitions is None:
            self._engine_definitions = self._dao.get_engines()
        in_error = dict()
        self._engines = dict()
        for engine in self._engine_definitions:
            if engine.engine not in self._engine_types:
                log.warning('Cannot find engine %s anymore', engine.engine)
                if engine.engine not in in_error:
                    in_error[engine.engine] = True
                    self.engineNotFound.emit(engine)
            self._engines[engine.uid] = self._engine_types[engine.engine](self, engine)
            self._engines[engine.uid].online.connect(self._force_autoupdate)
            self.initEngine.emit(self._engines[engine.uid])

    def _force_autoupdate(self):
        if self.updater.get_next_poll() > 60 and self.updater.get_last_poll() > 1800:
            self.updater.force_poll()

    def get_default_nuxeo_drive_folder(self):
        """
        Find a reasonable location for the root Nuxeo Drive folder

        This folder is user specific, typically under the home folder.

        Under Windows, try to locate My Documents as a home folder, using the
        win32com shell API if allowed, else falling back on a manual detection.

        Note that we need to decode the path returned by os.path.expanduser with
        the local encoding because the value of the HOME environment variable is
        read as a byte string. Using os.path.expanduser(u'~') fails if the home
        path contains non ASCII characters since Unicode coercion attempts to
        decode the byte string as an ASCII string.
        """

        folder = ''
        if sys.platform == 'win32':
            from win32com.shell import shell, shellcon
            try:
                folder = shell.SHGetFolderPath(
                    0, shellcon.CSIDL_PERSONAL, None, 0)
            except:
                """
                In some cases (not really sure how this happens) the current user
                is not allowed to access its 'My Documents' folder path through
                the win32com shell API, which raises the following error:
                com_error: (-2147024891, 'Access is denied.', None, None)
                We noticed that in this case the 'Location' tab is missing in the
                Properties window of 'My Documents' accessed through the
                Explorer.
                So let's fall back on a manual (and poor) detection.
                WARNING: it's important to check 'Documents' first as under
                Windows 7 there also exists a 'My Documents' folder invisible in
                the Explorer and cmd / powershell but visible from Python.
                First try regular location for documents under Windows 7 and up
                """
                log.error('Access denied to the API SHGetFolderPath,'
                          ' falling back on manual detection')
                folder = os.path.expanduser(r'~\Documents')
                folder = unicode(folder.decode(ENCODING))

        if not folder:
            # Fall back on home folder otherwise
            folder = os.path.expanduser('~')
            folder = unicode(folder.decode(ENCODING))

        folder = self._increment_local_folder(folder, self.app_name)
        log.debug('Will use %r as default folder location', folder)
        return folder

    def _increment_local_folder(self, basefolder, name):
        folder = os.path.join(basefolder, name)
        num = 2
        while not self.check_local_folder_available(folder):
            folder = os.path.join(basefolder, name + ' ' + str(num))
            num += 1
            if num > 42:
                return ''
        return folder

    def open_local_file(self, file_path, select=False):  # TODO: Move to utils.py
        """
        Launch the local OS program on the given file / folder.

        :param file_path: The file URL to open.
        :param select: Hightlight the given file_path. Useful when
                       opening a folder and to select a file.
        """
        file_path = unicode(file_path)
        log.debug('Launching editor on %s', file_path)
        if sys.platform == 'win32':
            if select:
                win32api.ShellExecute(None, 'open', 'explorer.exe',
                                      '/select,' + file_path, None, 1)
            else:
                os.startfile(file_path)
        elif sys.platform == 'darwin':
            args = ['open']
            if select:
                args += ['-R']
            args += [file_path]
            subprocess.Popen(args)
        else:
            # TODO NXDRIVE-848: Select feature not yet implemented
            # TODO See https://bugs.freedesktop.org/show_bug.cgi?id=49552
            try:
                subprocess.Popen(['xdg-open', file_path])
            except OSError:
                # xdg-open should be supported by recent Gnome, KDE, Xfce
                log.error('Failed to find and editor for: %r', file_path)

    @property
    def device_id(self):
        # type: () -> unicode
        if not self.__device_id:
            self.__device_id = uuid.uuid1().hex
            self._dao.update_config('device_id', self.__device_id)
        return self.__device_id

    def get_proxy_settings(self):
        """ Fetch proxy settings from database. """

        return ProxySettings(dao=self._dao)

    def get_config(self, value, default=None):
        return self._dao.get_config(value, default)

    def set_config(self, key, value):
        Options.set(key, value, setter='manual', fail_on_error=False)
        return self._dao.update_config(key, value)

    def get_direct_edit_auto_lock(self):
        # Enabled by default, if app is frozen
        return self._dao.get_config(
            'direct_edit_auto_lock', str(int(Options.is_frozen))) == '1'

    def set_direct_edit_auto_lock(self, value):
        self._dao.update_config('direct_edit_auto_lock', value)

    def get_auto_update(self):
        # Enabled by default, if app is frozen
        return self._dao.get_config(
            'auto_update', str(int(Options.is_frozen))) == '1'

    def set_auto_update(self, value):
        self._dao.update_config('auto_update', value)

    def get_auto_start(self):
        # Enabled by default, if app is frozen
        return self._dao.get_config(
            'auto_start', str(int(Options.is_frozen))) == '1'

    def _get_binary_name(self):  # TODO: Move to constants.py
        return 'ndrive'

    def generate_report(self, path=None):
        from .report import Report
        report = Report(self, path)
        report.generate()
        return report.get_path()

    def set_auto_start(self, value):
        self._dao.update_config("auto_start", value)
        if value:
            self.osi.register_startup()
        else:
            self.osi.unregister_startup()

    def get_beta_channel(self):
        return self._dao.get_config('beta_channel', '0') == '1'

    def set_beta_channel(self, value):
        self.set_config('beta_channel', value)
        # Trigger update status refresh
        self.refresh_update_status()

    def get_tracking(self):
        """
        Avoid sending statistics when testing or if the user does not allow it.
        """
        return (self._dao.get_config('tracking', '1') == '1'
                and not os.environ.get('WORKSPACE'))

    def set_tracking(self, value):
        self._dao.update_config('tracking', value)
        if value:
            self._create_tracker()
        elif self._tracker:
            self._tracker._thread.quit()
            self._tracker = None

    def get_tracker_id(self):
        return self._tracker.uid if self._tracker else ''

    def validate_proxy_settings(self, proxy_settings):
        conn = None
        url = "http://www.google.com"
        try:
            if proxy_settings.config in ('Manual', 'System'):
                proxies, _ = get_proxies_for_handler(proxy_settings)
                opener = urllib2.build_opener(urllib2.ProxyHandler(proxies),
                                              urllib2.HTTPBasicAuthHandler(),
                                              urllib2.HTTPHandler)
                conn = opener.open(url)
                conn.read()
            elif proxy_settings.config == 'Automatic':
                conn = urllib2.urlopen(proxy_settings.pac_url)
                if conn:
                    pac_script = conn.read()
                    pac_data = pypac.parser.PACFile(pac_script)
                    resolver = pypac.resolver.ProxyResolver(pac_data)
                    if self._engine_definitions:
                        # Check if every server URL can be resolved to some
                        # proxy server
                        for engine_def in self._engine_definitions:
                            url = self._engines[engine_def.uid].server_url
                            resolver.get_proxies(url)
                    else:
                        resolver.get_proxies(url)
        except Exception as e:
            log.error('Exception (%s) when validating proxy server for %s',
                      e, url)
            return False
        finally:
            if conn:
                conn.close()
        return True

    def set_proxy_settings(self, proxy_settings, force=False):
        if force or self.validate_proxy_settings(proxy_settings):
            proxy_settings.save(self._dao)
            self.refresh_proxies(proxy_settings)
            log.info("Proxy settings successfully updated: %r", proxy_settings)
            return ""
        return "PROXY_INVALID"

    def refresh_proxies(self, proxy_settings=None):
        """ Refresh current proxies with the given settings. """

        url = 'http://www.google.com'
        # If no proxy settings passed fetch them from database
        proxy_settings = (proxy_settings if proxy_settings is not None
                          else self.get_proxy_settings())
        if proxy_settings.config in ('Manual', 'System'):
            self.proxies['default'], self.proxy_exceptions = \
                get_proxies_for_handler(proxy_settings)
        elif proxy_settings.config == 'Automatic':
            if self._engine_definitions:
                for engine_def in self._engine_definitions:
                    server_url = self._engines[engine_def.uid].server_url
                    self.proxies[server_url] = proxy_settings.get_proxies_automatic(server_url)
                    log.trace('Setting proxy for %s to %r', server_url,
                              self.proxies[server_url])
            else:
                self.proxies['default'] = \
                    proxy_settings.get_proxies_automatic(url)
        else:
            self.proxies['default'], self.proxy_exceptions = {}, None

        log.trace('Effective proxy: %r', self.proxies['default'])
        self.proxyUpdated.emit(proxy_settings)

    @staticmethod
    def get_system_pac_url():
        """ Get the proxy auto config (PAC) URL, if present. """

        regkey = r'Software\Microsoft\Windows\CurrentVersion\Internet Settings'
        if AbstractOSIntegration.is_windows():
            # Use the registry
            settings = _winreg.OpenKey(_winreg.HKEY_CURRENT_USER, regkey)
            try:
                return str(_winreg.QueryValueEx(settings, 'AutoConfigURL')[0])
            except OSError as e:
                if e.errno not in (2,):
                    log.exception('Error retrieving PAC URL')
            finally:
                _winreg.CloseKey(settings)
        elif AbstractOSIntegration.is_mac():
            # Use SystemConfiguration library
            try:
                config = SystemConfiguration.SCDynamicStoreCopyProxies(None)
            except AttributeError:
                # It may happen on rare cases. The next call will work.
                return

            if ('ProxyAutoConfigEnable' in config
                    and 'ProxyAutoConfigURLString' in config):
                # 'Auto Proxy Discovery' or WPAD is not supported yet
                # Only 'Automatic Proxy configuration' URL setting is supported
                if not ('ProxyAutoDiscoveryEnable' in config
                        and config['ProxyAutoDiscoveryEnable'] == 1):
                    return str(config['ProxyAutoConfigURLString'])

    def retreive_system_proxies(self, server_url):
        """
        Gets the proxy server if system is configured with PAC URL.

        @param server_url: The server URL for which the proxy server
                           should be determined
        """

        proxies = None
        if self.proxies and 'default' in self.proxies:
            proxies = self.proxies['default']
        if proxies is None:
            pac_url = self.get_system_pac_url()
            if pac_url:
                # A PAC URL is configured
                proxy_settings = ProxySettings()
                proxy_settings.config = 'Automatic'
                proxy_settings.pac_url = pac_url
                proxies = proxy_settings.get_proxies_automatic(server_url)
                log.trace('System proxy (from PAC) retreived: %r', proxies)
        return proxies

    def get_proxies(self, server_url):
        """
        Returns the proxy server address based on server_url.
        For Automatic Proxy Configuraiton (.pac) there can be
        different proxy server for different out bound URLs.

        :param server_url: The URL of the server
        :return: The proxy settings required for the specific server
        """

        proxy_settings = self.get_proxy_settings()
        proxies = {}

        if proxy_settings.config == 'Manual':
            if self.proxies and 'default' in self.proxies:
                proxies = self.proxies['default']
        elif proxy_settings.config == 'System':
            proxies = self.retreive_system_proxies(server_url)
        elif proxy_settings.config == 'Automatic':
            if self.proxies.get(server_url) is None:
                self.proxies[server_url] = \
                    proxy_settings.get_proxies_automatic(server_url)
            proxies = self.proxies[server_url]

        return proxies

    def _get_default_server_type(self):  # TODO: Move to constants.py
        return "NXDRIVE"

    def bind_server(self, local_folder, url, username, password, token=None,
                    name=None, start_engine=True, check_credentials=True):
        if name is None:
            name = self._get_engine_name(url)
        binder = namedtuple('binder', ['username', 'password', 'token', 'url',
                                       'no_check', 'no_fscheck'])
        binder.username = username
        binder.password = password
        binder.token = token
        binder.no_check = not check_credentials
        binder.no_fscheck = False
        binder.url = url
        return self.bind_engine(self._get_default_server_type(), local_folder,
                                name, binder, starts=start_engine)

    def _get_engine_name(self, server_url):
        import urlparse
        urlp = urlparse.urlparse(server_url)
        return urlp.hostname

    def check_local_folder_available(self, local_folder):
        if self._engine_definitions is None:
            return True
        if not local_folder.endswith('/'):
            local_folder = local_folder + '/'
        for engine in self._engine_definitions:
            other = engine.local_folder
            if not other.endswith('/'):
                other = other + '/'
            if other.startswith(local_folder) or local_folder.startswith(other):
                return False
        return True

    def update_engine_path(self, uid, local_folder):
        # Dont update the engine by itself,
        # should be only used by engine.update_engine_path
        if uid in self._engine_definitions:
            # Unwatch old folder
            self.osi.unwatch_folder(self._engine_definitions[uid].local_folder)
            self._engine_definitions[uid].local_folder = local_folder
        # Watch new folder
        self.osi.watch_folder(local_folder)
        self._dao.update_engine_path(uid, local_folder)

    def bind_engine(self, engine_type, local_folder, name, binder, starts=True):
        """Bind a local folder to a remote nuxeo server"""
        if name is None and hasattr(binder, 'url'):
            name = self._get_engine_name(binder.url)
        if hasattr(binder, 'url'):
            url = binder.url
            if '#' in url:
                # Last part of the url is the engine type
                engine_type = url.split('#')[1]
                binder.url = url.split('#')[0]
                log.debug('Engine type has been specified in the'
                          ' url: %s will be used', engine_type)

        if not self.check_local_folder_available(local_folder):
            raise FolderAlreadyUsed()

        if engine_type not in self._engine_types:
            raise EngineTypeMissing()

        if self._engines is None:
            self.load()

        if not local_folder:
            local_folder = self.get_default_nuxeo_drive_folder()
        local_folder = normalized_path(local_folder)
        if local_folder == self.nxdrive_home:
            # Prevent from binding in the configuration folder
            raise FolderAlreadyUsed()

        uid = uuid.uuid1().hex

        # Watch folder in the file explorer
        self.osi.watch_folder(local_folder)
        # TODO Check that engine is not inside another or same position
        engine_def = self._dao.add_engine(engine_type, local_folder, uid, name)
        try:
            self._engines[uid] = self._engine_types[engine_type](
                self, engine_def, binder=binder)
        except Exception as e:
            log.exception('Engine error')
            try:
                del self._engines[uid]
            except KeyError:
                pass
            self._dao.delete_engine(uid)
            # TODO Remove the DB?
            raise e

        self._engine_definitions.append(engine_def)
        # As new engine was just bound, refresh application update status
        self.refresh_update_status()
        if starts:
            self._engines[uid].start()
        self.newEngine.emit(self._engines[uid])

        # NXDRIVE-978: Update the current state to reflect the change in
        # the systray menu
        self._pause = False

        return self._engines[uid]

    def unbind_engine(self, uid):
        if self._engines is None:
            self.load()
        # Unwatch folder
        self.osi.unwatch_folder(self._engines[uid].local_folder)
        self._engines[uid].suspend()
        self._engines[uid].unbind()
        self._dao.delete_engine(uid)
        # Refresh the engines definition
        del self._engines[uid]
        self.dropEngine.emit(uid)
        self._engine_definitions = self._dao.get_engines()

    def unbind_all(self):
        if self._engines is None:
            self.load()
        for engine in self._engine_definitions:
            self.unbind_engine(engine.uid)

    def dispose_db(self):
        if self._dao is not None:
            self._dao.dispose()

    def dispose_all(self):
        for engine in self.get_engines().values():
            engine.dispose_db()
        self.dispose_db()

    def get_engines(self):  # TODO: Remove
        return self._engines

    @property
    def version(self):
        return __version__

    def is_started(self):  # TODO: Remove
        return self._started

    def is_syncing(self):
        syncing_engines = []
        for uid, engine in self._engines.items():
            if engine.is_syncing():
                syncing_engines.append(uid)
        if syncing_engines:
            log.debug("Some engines are currently synchronizing: %s", syncing_engines)
            return True
        log.debug("No engine currently synchronizing")
        return False

    def get_root_id(self, file_path):
        ref = LocalClient.get_path_remote_id(file_path, 'ndriveroot')
        if ref is None:
            parent = os.path.dirname(file_path)
            # We can't find in any parent
            if parent == file_path or parent is None:
                return None
            return self.get_root_id(parent)
        return ref

    def ctx_access_online(self, file_path):
        """ Open the user's browser to a remote document. """

        log.debug('Opening metadata window for %r', file_path)
        try:
            url = self.get_metadata_infos(file_path)
        except ValueError:
            log.warning('The document %r is not handled by the Nuxeo server'
                        ' or is not synchronized yet.', file_path)
        else:
            self.open_local_file(url)

    def ctx_copy_share_link(self, file_path):
        """ Copy the document's share-link to the clipboard. """

        url = self.get_metadata_infos(file_path)
        log.info('Copied %r', url)
        if sys.platform == 'win32':
            win32clipboard.OpenClipboard()
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardText(url, win32clipboard.CF_UNICODETEXT)
            win32clipboard.CloseClipboard()
        else:
            cb = QApplication.clipboard()
            cb.clear(mode=cb.Clipboard)
            cb.setText(url, mode=cb.Clipboard)

    def ctx_edit_metadata(self, file_path):
        """ Open the user's browser to a remote document's metadata. """

        log.debug('Opening metadata window for %r', file_path)
        try:
            url = self.get_metadata_infos(file_path, edit=True)
        except ValueError:
            log.warning('The document %r is not handled by the Nuxeo server'
                        ' or is not synchronized yet.', file_path)
        else:
            self.open_local_file(url)

    def get_metadata_infos(self, file_path, edit=False):
        remote_ref = LocalClient.get_path_remote_id(file_path)
        if remote_ref is None:
            raise ValueError(
                'Could not find file %r as Nuxeo Drive managed' % file_path)

        root_id = self.get_root_id(file_path)
        root_values = root_id.split('|')
        try:
            engine = self.get_engines()[root_values[3]]
        except:
            raise ValueError(
                'Unknown engine %s for %r' % (root_values[3], file_path))

        return engine.get_metadata_url(remote_ref, edit=edit)

    def send_sync_status(self, path):
        for engine in self._engine_definitions:
            # Only send status if we picked the right
            # engine and if we're not targeting the root
            path = unicodedata.normalize('NFC', force_decode(path))
            if (path.startswith(engine.local_folder)
                    and not os.path.samefile(path, engine.local_folder)):
                r_path = path.replace(engine.local_folder, '')
                dao = self._engines[engine.uid]._dao
                state = dao.get_state_from_local(r_path)
                self.osi.send_sync_status(state, path)
                break

    def set_script_object(self, obj):  # TODO: Remove
        # Used to enhance scripting with UI
        self._script_object = obj

    def _create_script_engine(self):
        from .scripting import DriveScript
        self._script_engine = QScriptEngine()
        if self._script_object is None:
            self._script_object = DriveScript(self)
        self._script_engine.globalObject().setProperty("drive", self._script_engine.newQObject(self._script_object))

    def execute_script(self, script, engine_uid=None):
        if self._script_engine is None:
            self._create_script_engine()
            if self._script_engine is None:
                return
        self._script_object.engine_uid = engine_uid
        log.debug("Will execute '%s'", script)
        result = self._script_engine.evaluate(script)
        if self._script_engine.hasUncaughtException():
            log.debug("Execution exception: %r", result.toString())