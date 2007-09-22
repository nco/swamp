# $Id$

"""
server - Contains 'top-level' code for swamp server instances


"""
# Copyright (c) 2007 Daniel Wang
# This file is part of SWAMP.
# SWAMP is released under the GNU General Public License version 3 (GPLv3)

import os
import threading 

# (semi-) third-party module imports
import SOAPpy

# SWAMP imports 
from swamp import log
from swamp_common import *
from swamp_config import Config     
import swamp.inspector as inspector
import swamp.soapi as soapi


class LaunchThread(threading.Thread):
    def __init__(self, launchFunc, updateFunc):
        threading.Thread.__init__(self)
        self.launchFunc = launchFunc
        self.updateFunc = updateFunc
        
    def run(self):
        self.updateFunc(self) # put myself as placeholder
        self.updateFunc(self.launchFunc()) # update with real token


class WorkerConnector(threading.Thread):
    def __init__(self, target, offer):
        """
        target is a tuple: (url, certificate)
          url: A string containing the SOAP url for the master
          certificate: An opaque object to present to the master.  Masters
          should only accept registrations from certificates they trust.
        offer is a tuple: (url, slots)
          url: A string containing the SOAP url for the offering interface
          slots: Scheduling slot count
          (consider providing CPU and I/O metrics, as well as a catalog
        """
        threading.Thread.__init__(self)
        self._target = target
        self._offer = offer
        self.active = True
        self._connected = False
        self._timeStart = time.time()
        self._timeLastAttempt = None
        self.exitJustification = None
        self.timeBetweenAttempts = 30 # 30 seconds
        self.timeToGiveUp = 1800 # 30 minutes
        self.maxSleep = 2
        
    def run(self):
        while self.active:
            # maintain a connection
            if (not self._connected) and (not self._timeout()):
                self._connect()                
        pass

    def _timeout(self):
        """return True if we have timed out."""
        # Timer is reset at:
        # a) construction/initialization,
        # b) server disconnects.
        if (time.time() - self._timeStart) >= self.timeToGiveUp:
            self.active = False
            self.exitJustification = "Timeout: giving up after %d seconds" % (
                self.timeToGiveUp)
            return True
        return False
    
    def _connect(self):
        """attempt a connection, if we've waited long enough."""
        if self._timeLastAttempt:
            waittime =  time.time() - self._timeLastAttempt
            if waittime >= self.timeBetweenAttempts:
                self._tryConnect()
            else:
                remaining = self.timeBetweenAttempts - waittime
                if remaining > self.maxSleep:
                    time.sleep(self.maxSleep)
                else:
                    time.sleep(remaining)
                
        pass 

    def _tryConnect(self):
        """actually, make an attempt at connecting."""
        server = SOAPpy.SOAPProxy(self._target[0])
        # make some soap
        # connect and register my url and slot count.
        # in the future, register my catalog
        try:
            ack = server.registerWorker(self._target[1], self._offer)
        except:
            ack = False
        if not ack: # or some other failure mode
            # Characterize failure
            # fail- do not try again
            # fail- try again later
            # timeout - try again later
            return
        # on success, set connected.
        self._connected = True
        
    
class JobManager:
    """JobManager manages slave tasks run on this system.
    We will want to add contexts so that different tasks do not collide in
    files, but it's not needed right now for benchmarking, and will
    complicate debugging"""
        
    def __init__(self, cfgName, overrides):
        if cfgName:
            self.config = Config(cfgName)
        else:
            self.config = Config()
        self.config.read()
        self.config.update(overrides)
        
        self.config.dumpSettings(log, logging.DEBUG)
        self._setupLogging(self.config)
        
        self.jobs = {} # dict: tokens -> jobstate


        self.exportPrefix = "http://%s:%d/%s" % (self.config.slaveHostname,
                                                 self.config.slavePort,
                                                 self.config.slavePubPath)
        self.token = 0
        self.tokenLock = threading.Lock()
        self._modeSetup("worker")
                               
        pass

    def _modeSetup(self, mode):
        if mode == "worker":
            scratchSub = "s"
            bulkSub = "b"
            self.fileMapper = FileMapper("slave%d"%os.getpid(),
                                         self.config.execSourcePath,
                                         self.config.execScratchPath,
                                         self.config.execBulkPath)
            self.localExec = LocalExecutor(NcoBinaryFinder(self.config),
                                           self.fileMapper)

            # prefixes for remapping.
            self.scratchExportPref = self.exportPrefix + scratchSub + "/"
            self.bulkExportPref = self.exportPrefix + bulkSub + "/"

            self.publishedFuncs = [self.reset, self.slaveExec,
                                   self.pollState, self.pollStateMany,
                                   self.pollOutputs,
                                   self.discardFile, self.discardFiles,
                                   self.ping
                                   ]
            self.publishedPaths = [(self.config.slavePubPath + scratchSub,
                                    self.config.execScratchPath),
                                   (self.config.slavePubPath + bulkSub,
                                    self.config.execBulkPath)]
            
            pass
        elif mode == "master":
            log.error("Frontend/master code not migrated yet..")
            pass
        else:
            log.error("Invalid server mode, don't know what to do.")
            print 'Panic! Invalid server mode, expecting "worker" or "master"'
            return
    
    def _setupLogging(self, config):
        cfile = logging.FileHandler(config.logLocation)
        formatter = logging.Formatter('%(name)s:%(levelname)s %(message)s')
        cfile.setFormatter(formatter)
        log.addHandler(cfile)
        log.setLevel(config.logLevel)
        log.info("Swamp slave logging at " + config.logLocation)
        

    def reset(self):
        # Clean up trash from before:
        # - For now, don't worry about checking jobs still in progress
        # - Delete all the physical files we allocated in the file mapper
        log.info("Reset requested")
        self.fileMapper.cleanPhysicals()
        log.info("Reset finish")
        
    def slaveExec(self, pickledCommand):
        cf = CommandFactory(self.config)
        p = cf.unpickleCommand(pickledCommand)
        self.tokenLock.acquire()
        self.token += 1
        token = self.token + 0
        self.tokenLock.release()
        log.info("received cmd: %s %d token=%d outs=%s"
                 % (p.cmd, p.referenceLineNum, token, str(p.outputs)))
        self._threadedLaunch(p, token)
        return token

    def _updateToken(self, token, etoken):
        self.jobs[token] = etoken
        

    def _threadedLaunch(self, cmd, token):

        launch = lambda : self.localExec.launch(cmd)
        update = lambda et: self._updateToken(token, et)
        thread = LaunchThread(launch, update)
        thread.start()
        return

    def pollState(self, token):
        if token not in self.jobs:
            time.sleep(0.2) # possible race
            if token not in self.jobs:
                log.warning("token not ready after waiting.")
                return None
        if isinstance(self.jobs[token], threading.Thread):
            return None # token not even ready, arg fetch.
        res = self.localExec.poll(self.jobs[token])
        if res is not None:
            log.info("Token %d returned %s" % (token, str(res)))
            return res
        else:
            return None

    def pollStateMany(self, tokenList):
        return map(self.pollState, tokenList)

    def actualToPub(self, f):
        relative = f.split(self.config.execScratchPath + os.sep, 1)
        if len(relative) < 2:
            relative = f.split(self.config.execBulkPath + os.sep, 1)
            return self.bulkExportPref + relative[1]
        else:
            return self.scratchExportPref + relative[1]
    
    def pollOutputs(self, token):
        assert token in self.jobs
        outs = self.localExec.actualOuts(self.jobs[token])
        outs += self.localExec.fetchedSrcs(self.jobs[token])
        log.debug("outs is " + str(outs) + " for " + str(token))
        l = map(lambda t: (t[0], self.actualToPub(t[1])), outs)
        log.debug("also outs is " + str(l))
        return l


    def discardFile(self, f):
        log.debug("Discarding "+str(f))
        self.fileMapper.discardLogical(f)

    def discardFiles(self, fList):
        log.debug("Bulk discard "+str(fList))
        #for f in fList:
        for i in range(len(fList)):
            self.fileMapper.discardLogical(fList[i])
        #map(self.fileMapper.discardLogical, fList)

    def ping(self):
        return "PONG %f" %time.time()

    def dangerousRestart(self):
         args = sys.argv #take the original arguments
         args.insert(0, sys.executable) # add python
         os.execv(sys.executable, args) # replace self with new python.

    def listenTwisted(self):
        self.config.serverInspectPath = "inspect"
        custom = [("inspect", inspector.newResource(self.config))]
        s = soapi.Instance((self.config.slaveHostname,
                            self.config.slavePort,
                            self.config.slaveSoapPath), 
                           self.publishedPaths,
                           self.publishedFuncs,
                           custom)
                 
        s.listenTwisted()

    
        
    pass # end class JobManager
 
def selfTest():
    pass

def pingServer(configFilename):
    soapi.pingTest(configFilename)

def startServer(configFilename, overrides={}):
    selfTest()

    #jm = soapi.JobManager(configFilename)
    
    jm = JobManager(configFilename, overrides)
    jm.listenTwisted()


    pass
