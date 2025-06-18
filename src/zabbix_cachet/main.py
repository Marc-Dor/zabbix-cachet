#!/usr/bin/env python3
"""
This script populated Cachet of Zabbix IT Services
"""
import sys
import os
import pathlib
import datetime
from dataclasses import dataclass
from typing import List, Union

import time
import threading
import logging

import requests
import yaml
import pytz

from zabbix_cachet.cachet import Cachet
from zabbix_cachet.excepltions import ZabbixNotAvailable, ZabbixCachetException, ZabbixServiceNotFound
from zabbix_cachet.zabbix import Zabbix, ZabbixService

__author__ = 'Artem Aleksandrov <qk4l()tem4uk.ru>'
__license__ = """The MIT License (MIT)"""
__version__ = '2.1.4'


@dataclass
class ConfigTemplates:
    acknowledgement: str = "{message}\n\n###### {ack_time} by {author}\n\n______\n"
    acknowledgement_time_strftime: str = '%b %d, %H:%M %z'
    investigating: str = ''
    resolving: str = ''


class Config:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(Config, cls).__new__(cls, *args, **kwargs)
        return cls._instance

    def __init__(self):
        if not hasattr(self, 'initialized'):
            if os.getenv('CONFIG_FILE') is not None:
                self.config_file = pathlib.Path(os.environ['CONFIG_FILE'])
            else:
                self.config_file = pathlib.Path.cwd() / 'config.yml'
            if not self.config_file.is_file():
                logging.error(
                    f"Config file {self.config_file} is absent. Set CONFIG_FILE to change path or create it there.")
                sys.exit(1)
            config = read_config(self.config_file)
            if not config:
                sys.exit(1)
            self.zabbix_config = config['zabbix']
            self.cachet_config = config['cachet']
            self.app_settings = config['settings']

            if self.app_settings.get('time_zone'):
                self.tz = pytz.timezone(self.app_settings['time_zone'])
            else:
                self.tz = None

            self.templates = ConfigTemplates(**config.get('templates'))
            self.initialized = True


@dataclass
class ZabbixCachetMap:
    cachet_component_id: int
    cachet_component_name: str

    cachet_group_id: Union[int, None] = None
    cachet_group_name: str = ''

    zbx_serviceid: str = None

    def __str__(self):
        return f"{self.cachet_group_name}/{self.cachet_component_name} - {self.zbx_serviceid}"


def triggers_watcher(service_map: List[ZabbixCachetMap], zapi: Zabbix, cachet: Cachet) -> bool:
    """
    Check zabbix triggers and update Cachet components
    Zabbix Priority:
        0 - (default) not classified;
        1 - information;
        2 - warning;
        3 - average;
        4 - high;
        5 - disaster.
    Cachet Incident Statuses:
        0 - Scheduled - This status is used for a scheduled status.
        1 - Investigating - You have reports of a problem, and you're currently looking into them.
        2 - Identified - You've found the issue, and you're working on a fix.
        3 - Watching - You've since deployed a fix, and you're currently watching the situation. # Does not use for now
        4 - Fixed

    Zabbix Trigger <> Cachet Incident mapping
        New - Investigating
        Acknowledged - Identified
        Resolved - Fixed
    @return: boolean
    """
    config = Config()
    for i in service_map:  # type: ZabbixCachetMap
        # inc_status = 1
        # comp_status = 1
        # inc_name = ''
        inc_msg = ''

        try:
            service = zapi.get_zabbix_service(serviceid=i.zbx_serviceid)
        except ZabbixServiceNotFound as err:
            logging.warning(f"Skip service with serviceid {i.zbx_serviceid} because it was not found in Zabbix: {err}")
            continue

        cache_component = cachet.get_component(i.cachet_component_id)
        if not cache_component:
            logging.error(f"Failed to get Cachet component with ID: {i.cachet_component_id}. Skip it")
            continue
        # Service not failed
        comp = cache_component.get('data', {})
        # Cachet 2.x  →  comp['status']
        # Cachet 3.x  →  comp['attributes']['status']['value']
        component_status = (
            comp.get('status') or
            comp.get('attributes', {}).get('status', {}).get('value')
        )

        component_incidents = cachet.get_incidents(i.cachet_component_id)

        # TODO: This only fixes one incident, right? Should loop through incidents with the right component-id
        if service.is_status_ok:
            # component in operational mode
            if str(component_status) == '1':
                continue

            # component not operational mode. Resolve it.
            for incident in component_incidents:
                # Incident already registered
                if incident['attributes']['status']['value'] != '4':
                    logging.debug(f"Found unfixed incident (id: {incident['id']}). Resolving it")
                    inc_msg = config.templates.resolving.format(
                        time=datetime.datetime.now(tz=config.tz).strftime('%b %d, %H:%M'),
                    ) + incident['attributes']['message']
                    cachet.upd_incident(incident['id'],
                                        status=4,
                                        component_id=i.cachet_component_id,
                                        component_status=1,
                                        message=inc_msg)
                    
            cachet.upd_components(i.cachet_component_id, status=1)
            
            # Continue with next service. This one is ok.
            continue

        # Service failed

        # TODO: For Services that don't have problem_tags, but have child-services, this completely breaks (It get's EVERY unresolved problem)
        #triggers = zapi.get_trigger(tags=service.problem_tags)

        problem_events = zapi.get_service_events(service.serviceid)

        logging.debug(f"problem_events of service {service.name}: " + str(problem_events))

        for problem_event in problem_events['problem_events']:
            zbx_event = zapi.get_event_info(problem_event['eventid'])
            trigger = zbx_event['relatedObject']

            inc_name = trigger['description']

            if zbx_event.get('acknowledged', '0') == '1':
                inc_status = 2
                for msg in zbx_event['acknowledges']:  # type: dict
                    author = msg.get('name', '') + ' ' + msg.get('surname', '')
                    ack_time = (datetime.datetime.fromtimestamp(int(msg['clock']), tz=config.tz).
                                strftime(config.templates.acknowledgement_time_strftime))
                    ack_msg = config.templates.acknowledgement.format(
                        message=msg['message'],
                        ack_time=ack_time,
                        author=author
                    )
                    if ack_msg not in inc_msg:
                        inc_msg = ack_msg + inc_msg
            else:
                inc_status = 1
            
            # TODO: Does this allow to change via Dashboard?
            if service.status >= 4:
                comp_status = 4
            elif service.status == 3:
                comp_status = 3
            else:
                comp_status = 2
            
            if not inc_msg and config.templates.investigating:
                zbx_event_clock = int(zbx_event.get('clock', 0))
                if zbx_event_clock:
                    zbx_event_time = datetime.datetime.fromtimestamp(zbx_event_clock, tz=config.tz).strftime(
                        '%b %d, %H:%M')
                else:
                    zbx_event_time = ''
                inc_msg = config.templates.investigating.format(
                    group=i.cachet_group_name,
                    component=i.cachet_component_name,
                    time=zbx_event_time,
                    trigger_description=trigger.get('comments', ''),
                    trigger_name=trigger.get('description', ''),
                )

            # Just in case when user sets investigating template to empty string
            if not inc_msg and trigger.get('comments'):
                inc_msg = trigger.get('comments')
            elif not inc_msg:
                inc_msg = trigger.get('description')

            if i.cachet_group_name:
                inc_name = i.cachet_group_name + ' | ' + inc_name

            component_incidents = cachet.get_incidents(i.cachet_component_id)

            logging.debug('Looking for unfixed cachet-incident "' + trigger.get('description') + '"...')
            found_unfixed_incident = False

            for incident in component_incidents:
                if trigger.get('description') in incident['attributes']['name']:

                    # Incident already registered
                    if incident['attributes']['status']['value'] != '4':
                        logging.debug(f"Found unfixed and name-matching incident. (id: {incident['id']})")
                        # Only incident message can change. So check if this has happened
                        if incident['attributes']['message'].strip() != inc_msg.strip():
                            cachet.upd_incident(incident['id'], message=inc_msg, status=inc_status,
                                                component_status=comp_status)
                            # TODO: Hotfix, because components don't get attached to incident https://github.com/cachethq/core/issues/156
                            cachet.upd_components(i.cachet_component_id, status=comp_status)
                        found_unfixed_incident = True
                    
                    # Stop looking through incidents once there's a name-matching one, no matter the status
                    break
            

            # Incident not registered
            if not found_unfixed_incident:
                cachet.new_incidents(name=inc_name, message=inc_msg, status=inc_status,
                                    component_id=i.cachet_component_id, component_status=comp_status)
                # TODO: Hotfix, because components don't get attached to incident https://github.com/cachethq/core/issues/156
                cachet.upd_components(i.cachet_component_id, status=comp_status)
    return True


def triggers_watcher_worker(service_map, interval, tr_event: threading.Event, zapi: Zabbix, cachet: Cachet):
    """
    Worker for triggers_watcher. Run it continuously with specific interval
    @param service_map: list of tuples
    @param interval: interval in seconds
    @param tr_event: treading.Event object
    @param zapi: Zabbix object
    @param cachet: Cachet object
    @return:
    """
    logging.info('start trigger watcher')
    while not tr_event.is_set():
        logging.info('Check status of Zabbix triggers')
        # Do not run if Zabbix is not available
        if zapi.get_version():
            try:
                triggers_watcher(service_map, zapi=zapi, cachet=cachet)
            except Exception as e:
                logging.error('triggers_watcher() raised an Exception. Something gone wrong')
                logging.error(e, exc_info=True)
        else:
            logging.error('Zabbix is not available. Skip checking...')
        time.sleep(interval)
    logging.info('end trigger watcher')


def init_cachet(services: List[ZabbixService], zapi: Zabbix, cachet: Cachet) -> List[ZabbixCachetMap]:
    """
    Init Cachet by syncing Zabbix service to it
    Also func create mapping batten Cachet components and Zabbix IT services
    :param services: list of ZabbixService
    :param cachet: Cachet object
    :param zapi: Zabbix object
    @return: list of tuples
    """
    # Zabbix Triggers to Cachet components id map
    data = []

    for zbx_service in services:
        cachet_group_id = None
        cachet_group_name = ''
        # Check if zbx_service has childes
        if zbx_service.children:
            cachet_group_name = zbx_service.name
            group = cachet.new_components_gr(name=cachet_group_name)
            cachet_group_id = group['id']

            for dependency in zbx_service.children:
                component = cachet.new_components(dependency.name, component_group_id=cachet_group_id,
                                                      description=dependency.description)
                # Create a map of Zabbix Trigger <> Cachet IDs
                zxb2cachet_i = ZabbixCachetMap(
                    zbx_serviceid=dependency.serviceid,
                    cachet_group_id=cachet_group_id,
                    cachet_group_name=cachet_group_name,
                    cachet_component_id=component['id'],
                    cachet_component_name=(component.get('name') or component.get('attributes', {}).get('name'))
                )
                data.append(zxb2cachet_i)
        else:
            if zbx_service.problem_tags:
                component = cachet.new_components(zbx_service.name, description=zbx_service.description)
            else:
                logging.warning(f'Zabbix Service with service id = {zbx_service.serviceid} does not have'
                                f' child service or problem_tags. Monitoring will not work for it')
                continue
            # Create a map of Zabbix Trigger <> Cachet IDs
            zxb2cachet_i = ZabbixCachetMap(
                zbx_serviceid=zbx_service.serviceid,
                cachet_group_id=cachet_group_id,
                cachet_group_name=cachet_group_name,
                cachet_component_id=component['id'],
                cachet_component_name=(component.get('name') or component.get('attributes', {}).get('name'))
            )
            data.append(zxb2cachet_i)
    return data


def read_config(config_f):
    """
    Read config file
    @param config_f: strung
    @return: dict of data
    """
    try:
        return yaml.safe_load(open(config_f, "r"))
    except (yaml.error.MarkedYAMLError, IOError) as e:
        logging.error(f"Failed to parse config file {config_f}: {e}")
    return None


def main():
    exit_status = 0
    config = Config()

    # Set Logging
    log_level = logging.getLevelName(config.app_settings['log_level'])
    log_level_requests = logging.getLevelName(config.app_settings['log_level_requests'])
    logging.basicConfig(
        level=log_level,
        format='%(asctime)s %(levelname)s: (%(threadName)s) %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S %Z'
    )
    logging.getLogger("requests").setLevel(log_level_requests)
    logging.info(f'Zabbix Cachet v.{__version__} started (config: {config.config_file})')
    inc_update_t = threading.Thread()
    event = threading.Event()
    try:
        zapi = Zabbix(config.zabbix_config['server'], config.zabbix_config['user'], config.zabbix_config['pass'],
                      config.zabbix_config['https-verify'])
        cachet = Cachet(config.cachet_config['server'], config.cachet_config['token'],
                        config.cachet_config['https-verify'])
        logging.info('Zabbix ver: {}. Cachet ver: {}'.format(zapi.version, cachet.version))
        zbxtr2cachet = ''
        while True:
            try:
                logging.debug('Getting list of Zabbix IT Services ...')
                it_services = zapi.get_itservices(config.app_settings['root_service'])
                logging.debug('Zabbix IT Services: {}'.format(it_services))
                # Create Cachet components and components groups
                logging.debug('Syncing Zabbix with Cachet...')
                zbxtr2cachet_new = init_cachet(it_services, zapi, cachet)
            except ZabbixNotAvailable:
                time.sleep(config.app_settings['update_comp_interval'])
                continue
            except ZabbixCachetException:
                zbxtr2cachet_new = False
            if not zbxtr2cachet_new:
                logging.error('Sorry, can not create Zabbix <> Cachet mapping for you. Please check above errors')
                # Exit if it's an initial run
                if not zbxtr2cachet:
                    sys.exit(1)
                else:
                    zbxtr2cachet_new = zbxtr2cachet
            else:
                logging.info('Successfully synced Cachet components with Zabbix Services')
            # Restart triggers_watcher_worker
            if zbxtr2cachet != zbxtr2cachet_new:
                zbxtr2cachet = zbxtr2cachet_new
                logging.info('Restart triggers_watcher worker')
                # TODO: Could failed
                logging.info(f'List of watching triggers {zbxtr2cachet}')
                event.set()
                # Wait until tread die
                while inc_update_t.is_alive():
                    time.sleep(1)
                event.clear()
                inc_update_t = threading.Thread(name='Trigger Watcher',
                                                target=triggers_watcher_worker,
                                                args=(zbxtr2cachet, config.app_settings['update_inc_interval'], event,
                                                      zapi, cachet))
                inc_update_t.daemon = True
                inc_update_t.start()
            time.sleep(config.app_settings['update_comp_interval'])
    except requests.exceptions.ConnectionError as err:
        logging.error(f"Failed to connect: {err}")
        exit_status = 1
    except KeyboardInterrupt:
        event.set()
        logging.info('Shutdown requested. See you.')
    except Exception as error:
        logging.exception(error)
        exit_status = 1
    sys.exit(exit_status)


if __name__ == '__main__':
    main()
