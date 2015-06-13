#!/usr/bin/env python
#
# run_thug.py
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston,
# MA  02111-1307  USA
#
# Author:   Pietro Delsante <pietro.delsante@gmail.com>
#           The Honeynet Project
#
import logging
import os
import re
import signal
import subprocess
import time

from datetime import datetime
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from main.models import *

import pymongo
from bson import ObjectId

client = pymongo.MongoClient()
db = client.thug

logger = logging.getLogger(__name__)

class TimeoutException(Exception):
    pass

class InvalidMongoIdException(Exception):
    pass

def timeout_handler(signum, frame):
    raise TimeoutException

class Command(BaseCommand):

    def _fetch_new_tasks(self):
        return Task.objects.filter(status__exact=STATUS_NEW).order_by('submitted_on')

    def _reset_processing(self):
        Task.objects.filter(status__exact=STATUS_PROCESSING).update(status=STATUS_NEW)

    def _mark_as_running(self, task):
        logger.debug("[{}] Marking task as running".format(task.id))
        task.started_on = datetime.now(pytz.timezone(settings.TIME_ZONE))
        task.status = STATUS_PROCESSING
        task.save()

    def _mark_as_failed(self, task):
        logger.debug("[{}] Marking task as failed".format(task.id))
        task.completed_on = datetime.now(pytz.timezone(settings.TIME_ZONE))
        task.status = STATUS_FAILED
        task.save()

    def _mark_as_completed(self, task):
        logger.debug("[{}] Marking task as completed".format(task.id))
        task.completed_on = datetime.now(pytz.timezone(settings.TIME_ZONE))
        task.status = STATUS_COMPLETED
        task.save()

    def urlid_to_url(self,document):
        document["url"] = db.urls.find_one({"_id":ObjectId(document["url_id"])})["url"]
        document.pop("url_id")
        return document

    def remove_analysis_id(self,document):
        document.pop("analysis_id")
        document.pop("_id")
        return document

    def id_to_old_id(self,document):
        document["old_id"]= document.pop("_id")
        return document

    def club_collections(self,analysis_id):
        analysis = db.analyses.find_one({"_id":ObjectId(analysis_id)})
        analysis["exploits"] = [self.remove_analysis_id(self.urlid_to_url(x)) for x in db.exploits.find({"analysis_id":ObjectId(analysis_id)})]
        analysis["codes"] = [self.remove_analysis_id(x) for x in db.codes.find({"analysis_id":ObjectId(analysis_id)})]
        analysis["behaviors"] = [self.remove_analysis_id(x) for x in db.behaviors.find({"analysis_id":ObjectId(analysis_id)})]
        analysis["certificates"] = [self.remove_analysis_id(urlid_to_url(x)) for x in db.certificates.find({"analysis_id":ObjectId(analysis_id)})]
        analysis["maec11"] = [self.remove_analysis_id(x) for x in db.maec11.find({"analysis_id":ObjectId(analysis_id)})]
        first_url = db.urls.find_one({"_id":ObjectId(analysis["url_id"])})
        first_url["old_id"] = first_url.pop("_id")
        analysis["url_map"] = [first_url] #for further grid_fs maps id to url
        analysis = self.urlid_to_url(analysis)
        analysis.pop("_id")
        #now cleaning connections
        #using urls instead of url_ids
        connections = [self.remove_analysis_id(x) for x in db.connections.find({"analysis_id":ObjectId(analysis_id)})]
        for x in connections:
            # x.pop("analysis_id")
            # x.pop()
            x["source_url"] = db.urls.find_one({"_id":ObjectId(x["source_id"])})["url"]
            temp = db.urls.find_one({"_id":ObjectId(x["destination_id"])})
            if temp not in analysis["url_map"]:
                temp["old_id"]= temp.pop("_id")
                analysis["url_map"].append(temp)
            x["destination_url"] = temp["url"]
            x.pop("source_id")
            x.pop("destination_id")

        analysis["connections"] = connections

        # preserving all grid_fs related
        # collections as it is

        analysis["locations"] = [self.id_to_old_id(x) for x in db.locations.find({"analysis_id":ObjectId(analysis_id)})]
        analysis["virustotal"] = [self.id_to_old_id(x) for x in db.virustotal.find({"analysis_id":ObjectId(analysis_id)})]
        analysis["honeyagent"] = [self.id_to_old_id(x) for x in db.honeyagent.find({"analysis_id":ObjectId(analysis_id)})]
        analysis["androguard"] = [self.id_to_old_id(x) for x in db.androguard.find({"analysis_id":ObjectId(analysis_id)})]
        analysis["peepdf"] = [x for x in db.peepdf.find({"analysis_id":ObjectId(analysis_id)})]
        
        return analysis

    def run_task(self, task):
        # Initialize args list for docker
        args = [
            "/usr/bin/sudo", "/usr/bin/docker", "run",
            "-a", "stdin",
            "-a", "stdout",
            "-a", "stderr",
            "-t",
            "pdelsante/thug",
            "/usr/bin/python", "/opt/thug/src/thug.py"
            ]

        # Base options
        if task.referer:
            args.extend(['-r', task.referer])
        if task.useragent:
            args.extend(['-u', task.useragent])

        # Proxy
        if task.proxy:
            args.extend(['-p', str(task.proxy)])

        # Other options
        if task.events:
            args.extend(['-e', task.events])
        if task.delay:
            args.extend(['-w', task.delay])
        if task.timeout:
            args.extend(['-T', task.timeout])
        if task.threshold:
            args.extend(['-t', task.threshold])
        if task.no_cache:
            args.extend(['-m'])
        if task.extensive:
            args.extend(['-E'])
        if task.broken_url:
            args.extend(['-B'])

        # Logging
        if task.verbose:
            args.extend(['-v'])
        elif task.quiet:
            args.extend(['-q'])
        if task.debug:
            args.extend(['-d'])
            if task.ast_debug:
                args.extend(['-a'])
        if task.http_debug:
            args.extend(['-g'])

        # External services
        if task.vtquery:
            args.extend(['-y'])
        if task.vtsubmit:
            args.extend(['-s'])
        if task.no_honeyagent:
            args.extend(['-N'])

        # Plugins
        if task.no_adobepdf:
            args.extend(['-P'])
        elif task.adobepdf:
            args.extend(['-A', task.adobepdf])
        if task.no_shockwave:
            args.extend(['-R'])
        elif task.shockwave:
            args.extend(['-S', task.shockwave])
        if task.no_javaplugin:
            args.extend(['-K'])
        elif task.javaplugin:
            args.extend(['-J', task.javaplugin])

        # Add URL to args
        args.append(task.url)

        logger.debug("[{}] Will run command: {}".format(task.id, " ".join(args)))
        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        signal.signal(signal.SIGALRM, timeout_handler)
        signal.alarm(5*60) # 5 minutes
        try:
            stdout, stderr = p.communicate()
            signal.alarm(0)  # reset the alarm
        except TimeoutException:
            logger.error("[{}] Execution was taking too long, killed".format(task.id))
            raise

        r = re.search(r'\[MongoDB\] Analysis ID: ([a-z0-9]+)\b', stdout)
        if r:
            logger.info("[{}] Got ObjectID: {}".format(task.id, r.group(1)))
            final_id = db.analysiscombo.insert(self.club_collections(r.group(1)))
            return final_id
        else:
            logger.error("[{}] Unable to get MongoDB analysis ID for the current task".format(task.id))
            raise InvalidMongoIdException("Unable to get MongoDB analysis ID for the current task")



    def handle(self, *args, **options):
        logger.info("Starting up run_thug daemon")
        # Reset any tasks left behind from previous runs
        logger.debug("Resetting any tasks left in STATUS_PROCESSING by previous runs")
        self._reset_processing()

        # Start main thread
        while True:
            logger.debug("Fetching new tasks")
            tasks = self._fetch_new_tasks()
            logger.debug("Got {} new tasks".format(len(tasks)))
            for task in tasks:
                self._mark_as_running(task)
                try:
                    task.object_id = self.run_task(task)
                except subprocess.CalledProcessError as e:
                    logger.exception("[{}] Got CalledProcessError exception: {}".format(task.id, e))
                    self._mark_as_failed(task)
                    continue
                except TimeoutException as e:
                    logger.exception("[{}] Got Timeout exception: {}".format(task.id, e))
                    self._mark_as_failed(task)
                    continue
                except InvalidMongoIdException as e:
                    logger.exception("[{}] Got InvalidMongoIdException exception: {}".format(task.id, e))
                    self._mark_as_failed(task)
                    continue
                except Exception as e:
                    logger.exception("[{}] Got exception: {}".format(task.id, e))
                    self._mark_as_failed(task)
                    continue

                self._mark_as_completed(task)

            # Sleep for 10 seconds when no pending tasks
            logger.info("Sleeping for {} seconds waiting for new tasks".format(10))
            time.sleep(10)