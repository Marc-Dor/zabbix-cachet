import sys
import logging

from dataclasses import dataclass, field
from typing import List, Dict, Union

import requests

import urllib3
from pyzabbix import ZabbixAPI, ZabbixAPIException

from zabbix_cachet.excepltions import InvalidConfig, ZabbixNotAvailable, ZabbixCachetException, ZabbixServiceNotFound


def pyzabbix_safe(fail_result=False):

    def wrap(func):
        def wrapperd_f(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except (requests.ConnectionError, ZabbixAPIException) as e:
                logging.error('Zabbix Error: {}'.format(e))
                return fail_result
        return wrapperd_f
    return wrap


@dataclass
class ZabbixService:
    name: str
    serviceid: str
    status: int
    children: List['ZabbixService'] = field(default_factory=list)
    problem_tags: List[dict] = field(default_factory=list)
    description: str = ''
    has_children: bool = False

    def __repr__(self):
        if self.is_status_ok:
            status_str = 'OK'
        else:
            status_str = 'Failed'
        return f"ZabbixITService {self.name} in status {status_str} ({self.status})"

    @property
    def is_status_ok(self) -> bool:
        return self.status == -1


class Zabbix:
    def __init__(self, server: str, user: str, password: str, verify: bool = True):
        """
        Init zabbix class for further needs
        :return: pyzabbix object
        """
        self.server = server
        self.user = user
        self.password = password
        # Enable basic HTTP auth, some installations can use it
        # s = requests.Session()
        # s.auth = (user, password)
        # self.zapi = ZabbixAPI(server, s)

        self.zapi = ZabbixAPI(server)
        self.zapi.session.verify = verify
        if not verify:
            urllib3.disable_warnings()
        self.zapi.login(user, password)
        self.version = self.get_version()

    @pyzabbix_safe()
    def get_version(self):
        """
        Get Zabbix API version.
        This method is using to check if Zabbix is available
        :return: str
        """
        version = self.zapi.apiinfo.version()
        return version
    
    @pyzabbix_safe({})
    def get_event_info(self, eventid: str) -> List[dict]:
        """
        https://www.zabbix.com/documentation/current/en/manual/api/reference/event/get
        Get information about an event and the trigger that caused it
        @param eventid: string
        @return: dict of data
        """
        zbx_event = self.zapi.event.get(
            eventids=eventid,
            select_acknowledges='extend',
            selectRelatedObject='extend'
        )
        if len(zbx_event) >= 1:
            # TODO: This should never happen, right?
            logging.error(f"Error - eventid {eventid} not unique? This should never happen!")
            return zbx_event[-1]
        return zbx_event

    @pyzabbix_safe([])
    def get_service(self, name: str = '', serviceid: Union[List, str] = None,
                    parentids: str = '') -> List[Dict]:
        """
        https://www.zabbix.com/documentation/current/en/manual/api/reference/service/get
        :return:
        """
        query = {
            'output': 'extend',
            'selectChildren': 'extend',
            'selectProblemTags': 'extend',
        }
        if name:
            services = self.zapi.service.get(**query, filter={'name': name})
        elif serviceid:
            services = self.zapi.service.get(**query, serviceids=serviceid)
        elif parentids:
            services = self.zapi.service.get(**query, parentids=parentids)
        else:
            services = self.zapi.service.get(**query)
        return services

    @pyzabbix_safe([])
    def get_service_events(self, serviceid: Union[List, str] = None) -> List[Dict]:
        """
        https://www.zabbix.com/documentation/current/en/manual/api/reference/service/get
        :return:
        """
        query = {
            'output': 'extend',
            'selectChildren': 'extend',
            'selectProblemEvents': 'extend',
        }
        return self.zapi.service.get(**query, serviceids=serviceid)

    def _init_zabbix_it_service(self, data: Dict) -> ZabbixService:
        """
        Create ZabbixITService from data returned by service.get
        :param data: Service object
            https://www.zabbix.com/documentation/current/en/manual/api/reference/service/object
        """
        logging.debug(f"Init ZabbixITService for {data.get('name')} with problem_tags {data.get('problem_tags', [])}")
        zabbix_it_service = ZabbixService(name=data.get('name'),
                                          serviceid=data.get('serviceid'),
                                          description=data.get('description', ''),
                                          status=int(data.get('status')),
                                          problem_tags=data.get('problem_tags', []),
                                          )
        if 'children' in data:
            zabbix_it_service.has_children = True
            child_services = self.get_service(parentids=zabbix_it_service.serviceid)
            zabbix_it_service.children.extend(map(self._init_zabbix_it_service, child_services))
        return zabbix_it_service

    @pyzabbix_safe([])
    def get_itservices(self, root_name: str = None) -> List[ZabbixService]:
        """
        Return tree of Zabbix IT Services
        root (hidden)
           - service1 (Cachet componentgroup)
             - child_service1 (Cachet component)
             - child_service2 (Cachet component)
           - service2 (Cachet componentgroup)
             - child_service3 (Cachet component)
        :param root_name: Name of service that will be root of tree.
                    Actually it will not be present in return tree.
                    It's using just as a start point , string
        :return: Tree of Zabbix IT Services
        :rtype: list
        """
        monitor_services = []
        if root_name:
            if not self.get_version():
                raise ZabbixNotAvailable('Zabbix is not available...')
            root_service = self.get_service(root_name)
            if not len(root_service) == 1:
                raise ZabbixCachetException(f'Can not find uniq "{root_name}" service in Zabbix')
            monitor_services = self._init_zabbix_it_service(root_service[0]).children
        else:
            raise InvalidConfig(f"settings.root_service should be defined in you config yaml file because "
                                    f"you use Zabbix version {self.version}")

            # TODO: Add support for Zabbix >= 6.0. Here's legacy code for reference:

            #if self.version_major < 6:
            #    services = self.get_service()
            #    for i in services:
            #        # Do not proceed non-root services directly
            #        if len(i['parents']) == 0:
            #            monitor_services.append(self._init_zabbix_it_service(i))

        return monitor_services

    def get_zabbix_service(self, serviceid: str) -> ZabbixService:
        """
        Method which primary should be used in zabbix-cachet code
        :param serviceid:
        :return:
        """
        service = self.get_service(serviceid=serviceid)
        if len(service) < 1:
            raise ZabbixServiceNotFound(f"No one service returned by serviceid - {serviceid}")
        return self._init_zabbix_it_service(service[0])

    # TODO: This should just be a Hotfix, may reconsider later
    def get_concated_problem_tags_with_children(self, service: ZabbixService) -> List[dict]:
        """
        Return all problem_tags of the given service **including all children**.
        Duplicate tags (same combination of 'tag' and 'value') are removed while
        preserving the original order: parent tags come first, followed by child tags.

        Args:
            service: Root ZabbixService whose tags should be gathered.

        Returns:
            A list of unique tag dictionaries.
        """
        from typing import Set, Tuple

        def _collect(svc: ZabbixService, seen: Set[Tuple[str, str]]) -> List[Dict]:
            tags: List[Dict] = []
            # Add this service's tags, skipping ones we've already seen
            for t in svc.problem_tags:
                key = (t.get("tag"), t.get("value"))
                if key not in seen:
                    seen.add(key)
                    tags.append(t)
            # Recurse into children
            for child in svc.children:
                tags.extend(_collect(child, seen))
            return tags

        return _collect(service, set())