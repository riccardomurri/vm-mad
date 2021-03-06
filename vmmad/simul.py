#! /usr/bin/env python
#
"""
Simulate an `Orchestrator` run given some parameters.
"""
# Copyright (C) 2011, 2012 ETH Zurich and University of Zurich. All rights reserved.
#
# Authors:
#   Christian Panse <cp@fgcz.ethz.ch>
#   Riccardo Murri <riccardo.murri@gmail.com>
#   Tyanko Aleksiev <tyanko.alexiev@gmail.com>
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
from __future__ import absolute_import

__docformat__ = 'reStructuredText'
__version__ = "1.0dev (SVN $Revision$)"


# stdlib imports
import argparse
from copy import copy
import csv
import os
import sys
import time
import types

# local imports
from vmmad import log
from vmmad.batchsys.replay import JobsFromFile
from vmmad.provider.libcloud import DummyCloud
from vmmad.orchestrator import Orchestrator, JobInfo, VmInfo

class OrchestratorSimulation(Orchestrator, DummyCloud):

    def __init__(self, max_vms, max_delta, max_idle, startup_delay,
                 output_file, csv_file, start_time, time_interval, cluster_size):
        # Convert starting time to UNIX time
        if start_time is not None and isinstance(start_time, types.StringTypes):
            start_time = time.mktime(time.strptime(start_time, "%Y-%m-%dT%H:%M:%S" ))

        # implement the `Cloud` interface to simulate a cloud provider
        DummyCloud.__init__(self, '1', '1')

        # init the Orchestrator part, using `self` as cloud provider and batch system interface
        Orchestrator.__init__(
            self,
            cloud=self,
            batchsys=JobsFromFile(csv_file, self.time, start_time),
            max_vms=max_vms,
            max_delta=max_delta,
            vm_start_timeout=time_interval*max(startup_delay, 10))

        # make cluster nodes already available at start
        self.cluster_size = cluster_size
        for n in xrange(cluster_size):
            # NOTE: use `Orchestrator.new_vm` here, as `self.new_vm` creates a *real VM*
            nodeid = ('clusternode-%d' % n)
            node = Orchestrator.new_vm(self,
                                       vmid=nodeid,
                                       state=VmInfo.READY,
                                       nodename=nodeid,
                                       ever_running=True)
            self.vms[nodeid] = node

        # Set simulation settings
        self.max_idle = max_idle
        self.startup_delay = startup_delay

        self.output_file = open(output_file, "wb")
        self.writer = csv.writer(self.output_file, delimiter=',')
        self.writer.writerow(
            ['#TimeStamp', 'Pending Jobs', 'Running Jobs', 'Started VMs', 'Idle VMS'])

        self.time_interval = int(time_interval)
        self._next_row = None

        # info about running VMs
        self._vmid = 0

        # no running jobs at the onset
        self._running = 0

        # if `starting_time` has not been set, then use earliest job
        # submission time as starting point
        self.starting_time = self.batchsys.start_time - self.time_interval
        log.info("Starting simulation at %s",
                 time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(self.starting_time)))


    def update_job_status(self):
        # do regular work
        Orchestrator.update_job_status(self)

        # count running jobs
        self._running = len([ job for job in self.jobs.itervalues()
                              if job.state == JobInfo.RUNNING ])

        # simulate 'ready' notification from VMs
        starting_vms = [ vm for vm in self.vms.values() if vm.state == VmInfo.STARTING ]
        for vm in starting_vms:
            # we use `vm.last_idle` as a countdown to the `READY` state for VMs:
            # it is initialized to `-startup_delay` and incremented at every pass
            if vm.last_idle >= 0:
                nodename = ("vm-%s" % vm.vmid)
                self.vm_is_ready(vm.auth, nodename)
            else:
                vm.last_idle += 1

        # simulate SGE scheduler starting a new job
        ready_vms = [ vm for vm in self.vms.values() if vm.state == VmInfo.READY ]
        for vm in ready_vms:
            if not vm.jobs:
                if not self.candidates:
                    break
                job = self.candidates.pop()
                job.state = JobInfo.RUNNING
                job.exec_node_name = vm.nodename
                job.running_at = self.time()
                self._running += 1
                vm.jobs.add(job.jobid)
                log.info("Job %s just started running on node %s (%s).",
                         job.jobid, vm.vmid, vm.nodename)


    def before(self):
        # XXX: this only works with `JobsFromFile`!
        if len(self.jobs) == 0 and len(self.batchsys.future_jobs) == 0:
            log.info("No more jobs, stopping here")
            self.output_file.close()
            sys.exit(0)

        vms = [ vm for vm in self.vms.values() if not vm.ever_running ]
        vm_count = len(vms)
        starting_vm_count = len([ vm for vm in vms if vm.state == VmInfo.STARTING ])
        ready_vms_count = len([ vm for vm in vms if vm.state == VmInfo.READY ])
        stopping_vms_count = len([ vm for vm in vms if vm.state == VmInfo.STOPPING ])
        idle_vm_count = len([ vm for vm in vms if vm.last_idle > 0 ])
        self.writer.writerow(
            #  timestamp,  pending jobs,          running jobs,   started VMs,    idle VMs,
            [self.time(),  len(self.candidates),  self._running,  len(self.vms)-self.cluster_size,  idle_vm_count])

        log.info(
            "At time %d: pending jobs %d, running jobs %d, total started VMs %d,"
            " starting VMs %d, ready VMs %d, idle VMs %d, stopping VMs %d",
            self.time(), len(self.candidates), self._running, len(self.vms),
            starting_vm_count, ready_vms_count, idle_vm_count, stopping_vms_count)


    def time(self):
        """
        Return the current time in the simulation as UNIX epoch.
        """
        return self.starting_time + self.cycle * self.time_interval


    def new_vm(self, **attrs):
        return Orchestrator.new_vm(self, ever_running=False, last_idle=-self.startup_delay)


    ##
    ## policy implementation interface
    ##
    def is_cloud_candidate(self, job):
        # every job is a candidate in this simulation
        return True

    def is_new_vm_needed(self):
        if len(self.candidates) > 2 * len(self.vms):
            return True

    def can_vm_be_stopped(self, vm):
        if (not vm.ever_running and (vm.last_idle > self.max_idle) and len(vm.jobs) == 0):
            return True
        else:
            return False


    ##
    ## (fake) cloud provider interface
    ##

    def start_vm(self, vm):
        DummyCloud.start_vm(self, vm)

    def update_vm_status(self, vms):
        DummyCloud.update_vm_status(self, vms)

    def stop_vm(self, vm):
        assert not vm.ever_running, (
            "Request to stop VM %s which is marked as 'ever running'"
            % vm.vmid)
        DummyCloud.stop_vm(self, vm)
        return True



if "__main__" == __name__:
    parser = argparse.ArgumentParser(description='Simulates a cloud orchestrator')
    parser.add_argument('--max-vms', '-mv', metavar='N', dest="max_vms", default=10, type=int, help="Maximum number of VMs to be started, default is %(default)s")
    parser.add_argument('--max-delta', '-md', metavar='N', dest="max_delta", default=1, type=int, help="Cap the number of VMs that can be started or stopped in a single orchestration cycle. Default is %(default)d.")
    parser.add_argument('--max-idle', '-mi', metavar='NUM_SECS', dest="max_idle", default=7200, type=int, help="Maximum idle time (in seconds) before swithing off a VM, default is %(default)s")
    parser.add_argument('--startup-delay', '-s', metavar='NUM_SECS', dest="startup_delay", default=60, type=int, help="Time (in seconds) delay before a started VM is READY. Default is %(default)s")
    parser.add_argument('--csv-file', '-csvf',  metavar='String', dest="csv_file", default="accounting.csv", help="File containing the CSV information, %(default)s")
    parser.add_argument('--output-file', '-o',  metavar='String', dest="output_file", default="main_sim.txt", help="File name where the output of the simulation will be stored, %(default)s")
    parser.add_argument('--cluster-size', '-cs',  metavar='NUM_CPUS', dest="cluster_size", default="20", type=int, help="Number of VMs, used for the simulation of real available cluster: %(default)s")
    parser.add_argument('--start-time', '-stime',  metavar='String', dest="start_time", default=-1, help="Start time for the simulation, default: %(default)s")
    parser.add_argument('--time-interval', '-timei',  metavar='NUM_SECS', type=int, dest="time_interval", default="3600", help="UNIX interval in seconds used as parsing interval for the jobs in the CSV file, default: %(default)s")
    parser.add_argument('--version', '-V', action='version',
                        version=("%(prog)s version " + __version__))
    args = parser.parse_args()
    OrchestratorSimulation(args.max_vms, args.max_delta, args.max_idle, args.startup_delay, args.output_file, args.csv_file, args.start_time, args.time_interval, args.cluster_size).run(0)
