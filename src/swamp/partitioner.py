# $Id$
"""
   partitioner -- a module containing logic for splitting commands into
   "good" partitions.

"""

# 
# Copyright (c) 2007 Daniel Wang, Charles S. Zender
# This source file is part of SWAMP.
# SWAMP is released under the GNU General Public License version 3 (GPLv3)
#

# Python dependencies:
from collections import deque
import copy
import cPickle as pickle
from itertools import imap
import operator


def statDagGraph(cmdList, record=set()):
    otuples = []
    for c in cmdList:
        i = c.inputs
        o = c.outputs
        ituples = []
        for f in i:
            ituples.append([f,id(c)])
        for f in o:
            ituples.append([id(c),f])
        tuples = map(lambda t: " -> ".join(map(lambda s: '"%s"'%str(s),t)), ituples)
        tuples = (filter(lambda t: t not in record, tuples))
        record.update(tuples)
        otuples.extend(tuples)
    return "\n".join(otuples)

def statDagGraphCmds(cmdList, record=set()):
    otuples = []
    for c in cmdList:
        i = c.parents
        o = c.children
        ituples = []
        for f in i:
            #ituples.append([f,id(c)])
            ituples.append([f.name,c.name])
        for f in o:
            ituples.append([c.name,f.name])
            #ituples.append([id(c),f])
        tuples = map(lambda t: " -> ".join(map(lambda s: '"%s"'%str(s),t)), ituples)
        tuples = (filter(lambda t: t not in record, tuples))
        record.update(tuples)
        otuples.extend(tuples)
    return "\n".join(otuples)

def statDagGraphOld(cmdList, record=set()):
    otuples = []
    for c in cmdList:
        i = c.inputs
        o = c.outputs
        ituples = []
        for f in i:
            ituples.append([f,id(c)])
        if not i:
            ituples.append([id(c)])
        for f in o:
            for f2 in ituples:
                if (id(f2),id(f)) not in record:
                    otuples.append(f2+[f])
                    record.add((id(f2),id(f)))
    return "\n".join(map(lambda t: " -> ".join(map(lambda s: '"%s"'%str(s),t)), otuples))



class CommandCluster:
    """A cluster of commands.
    This is a useful abstraction, because it represents a set of commands that can be scheduled for execution on a particular execution unit.
    Characteristics:
    -- Nodes are either "root" or "body".
    (forall n in root.parents, n not in cluster)
    (forall n in body.parents, n in cluster)
    Nodes may not be mixed root-body, as this means that we must block root execution until child mixed nodes have their external deps satisfied, or else the cluster would require internal synchronization. (Actually, the conservative route is not that bad-- the performance penalty is similar to the case where the cluster has multiple roots(which would get blocked similarly).
    
    These characteristics are in flux.
    """
    def __init__(self, cmds, roots):
        self.cmds = set(cmds)

        if not roots:
            #roots = self._computeRoots() # this computes 'pure' root nodes
            #print "compute roots"
            roots = self._computeExtDepNodes()
        self.roots = roots
        self.parentCmds = reduce(lambda x,y: x.union(y.parents), # O(n)
                                 roots, set())
        self.parents = set()
        self.children = set()
        pass
        
    def updateChildCmds(self):
        children = set()
        map(lambda c: children.add(c), self.cmds)
        self.childCmds = children.difference(self.cmds)
        pass
    def addParent(self, cluster):
        assert isinstance(cluster, CommandCluster)
        self.parents.add(cluster)
        cluster.addChild(self)
        return cluster

    def addChild(self, cluster):
        assert isinstance(cluster, CommandCluster)
        self.children.add(cluster)
        if self not in cluster.parents:
            cluster.addParent(self)
        return cluster

    def exciseSelf(self):
        for c in self.children:
            c.parents.remove(self)
        self.children.clear()
        for p in self.parents:
            p.children.remove(self)
        self.parents.clear()
        pass

    def prolificMembers(self):
        """Prolific members are members of this cluster with children
        outside the cluster."""

        prolific = filter(lambda c: not self.cmds.issuperset(c.children),
                          self.cmds)
        return prolific

    def leafMembers(self):
        """Leaf members are members of this cluster that have no children
        within the cluster."""
        leaves = filter(lambda c: not self.cmds.intersection(c.children),
                        self.cmds)
        return leaves
    
    def ready(self, finishedCmds):
        # If all of my roots are ready to run, then I am ready.
        f = set(finishedCmds) # safe, even if finishedCmds is a set

        return reduce(operator.and_,
                      map(lambda cmd: f.issuperset(cmd.parents),
                          self.roots),
                      True)
    def pickleSelf(self, listSanitize):
        # Shallow copy myself
        # What we need to do is to isolate the cluster from troublesome
        # outside references, and then call the normal command pickler.

        c = copy.copy(self)
        map(lambda a: delattr(c, a),
            filter(lambda a: hasattr(c, a),
                   ['parentCmds', 'parents', 'children', 'childCmds']))

        # Replace cmd list with 'safe' cmds
        (cdict, newc) = listSanitize(c.cmds)
        c.cmds = newc
        pickle.dumps(newc)
        # convert root list
        c.roots = map(lambda cm: cdict[cm], c.roots)        

        return pickle.dumps(c)

    def _computeExtDepNodes(self):
        return filter(lambda cmd: # either no parents, or have ext parents
                      (0 == len(cmd.parents)) or
                      len(filter(lambda c: c not in self.cmds,
                                 cmd.parents)),
                      self.cmds)
    def _computeRoots(self): # O(n) 
        return filter(lambda cmd: 0 == len(filter(lambda c: c in self.cmds,
                                                  cmd.parents)),
                      self.cmds)
    
    def __contains__(self, item): ## O(1)?
        return item in self.cmds

    # make iterable, but prefer direct access on self.cmds for set ops
    def __iter__(self): 
        return self.cmds.__iter__()
    def __len__(self):
        return len(self.cmds)

def unpickleCluster(pickled, cmdUnpickle):
    # Fixup is probably not needed.
    c = pickle.loads(pickled)
    return c

class PlainPartitioner:
    """Find partitions based on the subtrees of each root (parent-less) node.
    Find subtrees, then intersections.
    Goal: convert a normal flowgraph into a graph of clusters.  This will aid
    in coarser-grained work distribution, which should result in reduced
    management overhead and naturally better locality."""
    def __init__(self, cmdList):
        self.cmdList = cmdList
        self.cluster = CommandCluster(cmdList, None)
        self.log = []
        self.ready = None

        pass

    def computeRootsChildren(self):
        finished = set()
        ready = lambda cmd: 0 == len(filter(lambda c: c not in finished,
                                            cmd.parents))
        r = []
        q = []
        for c in self.cmdList:
            if ready(c):
                r.append(c)
            else:
                q.append(c)
        return (r,q)

    def computeRoots(self):
        return filter(lambda cmd: 0 == len(cmd.parents), self.cmdList)

    def computeChildren(self, root):
        """perform DFS search of root's descendents"""
        d = deque()
        bag = set()
        d.append(root)
        while d:
            elem = d.pop()
            bag.add(elem)
            newElems = set(elem.children).difference(bag)
            d.extend(newElems)
        return bag

    def rootSplit(self, cluster):
        #rc = map(lambda n: (n, self.computeChildren(n)), cluster.roots) # O(n)
        rc = map(lambda n: CommandCluster(self.computeChildren(n),[n]),
                 cluster.roots) # O(n)
        # Fix cluster dependencies: (it's unclear whether we should
        # bother tracking these now, or just make everything
        # consistent when we actually need the info.)
        # For debugging, it helps to have a graph.

        parents = cluster.parents
        for c in rc: # for each new root cluster, connect it with its parent.
            rnodeparents = c.parentCmds # parent commands of this cluster
            rcparents = reduce(lambda n0,n:
                               n0.union(filter(lambda c:
                                                n in c, parents)),
                               rnodeparents, set())
            map(c.addParent, rcparents) # add the cluster parents
            
        children = cluster.parents  
        for c in rc: # for each new root cluster, connect it with its children
            c.updateChildCmds()
            nchildren  = c.childCmds # child commands of this cluster
            rcchildren = reduce(lambda n0,n:
                                n0.union(filter(lambda c:
                                                 n in c, children)),
                               nchildren, set())
            map(c.addParent, rcchildren) # add the cluster parents
        cluster.exciseSelf()
            
        # now, apply pairwise intersection.  Don't compute all
        # intersects first because that's expensive and
        # usually not necessary.
        isets = []
        for i0 in range(len(rc)):
            l = rc[i0].cmds
            for i1 in range(i0+1,len(rc)):
                # FIXME: would be nice to link the clusters here.
                r = rc[i1].cmds
                intersect = l.intersection(r)
                if not intersect:
                    continue
                iset = CommandCluster(intersect,None)
                l.difference_update(iset.cmds)
                iset.addParent(rc[i0])
                
                for i2 in range(i1,len(rc)):
                    old = len(rc[i2])
                    rc[i2].cmds.difference_update(iset)
                    if len(rc[i2]) < old:
                        iset.addParent(rc[i2])
                # We remove the intersection from the other root sets
                # to prevent duplicate isets from being created,
                # and update ancestry if needed.
                
                isets.append(iset)
                self.log.append("split intersect of %d and %d (%d: %d)" %
                                (i0, i1, len(rc), id(cluster)))
        # Now, we have fully-independent root clusters and a set of
        # child clusters.
        return (rc, isets)

    def compute(self):
        """perform partitioning according to the chosen parameters FIXME"""
        # some possible parameters:
        # minimum size: min node count for a cluster
        #  (should be small, or some fraction of total graph size)
        # num splits: desired number of resultant partitions. Partitioning will continue until there are no more "parallelizing splits", or the total partition count is >= num splits
        minSplits = 3
        clustermetalist = []
        (roots, inters) = self.rootSplit(self.cluster)
        clustermetalist.append(roots)
        if (len(roots) + len(inters)) < minSplits:
            # split intersects.
            inters = map(self.rootSplit, inters)
            clustermetalist.append(inters[0])
            clustermetalist.append(inters[1])
        else:
            clustermetalist.append(inters)
            
        print "nodes", len(self.cluster)
        print "roots", len(roots)
        self.ready = clustermetalist
        # The metalist is a list of lists of clusters.
        # list[0] is a list of clusters that are ready for execution.
        # list[1] is a list of clusters that are ready after all clusters
        # in list[0] are complete.  Some or all clusters may be ready
        # earlier, but each cluster requires some finite progress in one
        # or more clusters in list[0], otherwise the cluster could be
        # placed in list[0].
        # list[i+1] is related to list[i] similarly as list[1] is related
        # to list[0]
        open("pass1.dot","w").write(self.makeStateGraph("pass1",roots))
        pass


    def makeStateGraph(self, label, roots):
        clusstr = [
            "digraph %s {" % label]
        dagLog = set()
        d = deque()
        d.extend(roots)
        bag = set()
        while d:
            elem = d.popleft()
            clusstr.append("subgraph cluster%s { %s }" %(
                id(elem),
                str(statDagGraph(elem.cmds, dagLog))))
            bag.add(elem)
            newElems = set(elem.children).difference(bag)
            d.extend(newElems)
        clusstr.append(" }")
        return "\n".join(clusstr)

    def oldCompute(self):
        r = self.computeRoots() # this is O(n)
        
        rc = map(lambda n: (n, self.computeChildren(n)), r) # O(n)
        # now I have a list of roots and their corresponding sets.
        # now compute the intersection. (between O(kn) and O(n^2)
        # This code does ~2x required work because it computes the
        # symmetric intersections.
        intersects = map(lambda r: (r[0], r[1], map(lambda r2: r[1].intersection(r2[1]),
                                       filter(lambda r1:r1!=r, rc))),rc)
        # actually, I think we can skip a lot of this.  We want to pull the intersection off *anyway*. So, how about it if we just pull off the first intersection we find.  Sort the list by tree size, and then pull the largest, find its intersections, and pull the largest intersection off.  This creates two or three new trees.
        print len(r), "roots"
        
        print "\n".join(map(lambda t: "%d : %d children, %s" %(id(t[0]),
                                                               len(t[1]),
                                                               str([len(x) for x in t[2]])
                                                               ), intersects))
        pass

    def result(self):
        """return a list of connected *clusters* which can be scheduled."""
        if not self.ready:
            self.compute()
        return self.ready
        
class Bipartitioner:
    """Splits an approximately-min-cut partition of a flow graph."""
    def __init__(self, cmdList):
        tolerance = 0.2
        self.original = cmdList
        self.passcount = 0

        total = len(cmdList)
        self._limits = ((0.5-tolerance)*total,(0.5+tolerance)*total)
        self._halftotal = total/2
        # arbitrarily split from ordered sequence
        self.sets = [set(cmdList[:self._halftotal]),
                     set(cmdList[self._halftotal:])]
        for i in range(6):
            state = self._makePass()
            self.passcount += 1
            print "iteration gain: %d, sizes: %d %d" %(state[0],
                                                       len(state[1]),
                                                       len(state[2]))
            if state[0] < 0:
                print "No gain, no more passes needed."
                break
            self.sets = [state[1],state[2]]
            self.savecopy = [state[1].copy(), state[2].copy()]
        self.sets = self.savecopy
        self._writeSetState("stage%d_%d"%(self.passcount,0))
        
    def _makePass(self):
        self._locked = set() # locked nodes
        # while cutsize is reduced
        # while valid moves exist
        # use bucket data to find unlocked node in each partition that most improves cutsize
        self._makeBuckets(self.sets)
        ba = self._buckets[0]
        bb = self._buckets[1]
        
        #print "Buckets One", ba
        #print "Bucket Two", bb
        self._writeSetState("stage%d_0"%(self.passcount))
        print "(%d, %d)" %(tuple(map(len,self.sets)))
        def getMax(bucket):
            while True:
                m = max(bucket)
                c = bucket[m]
                if c:
                    print bucket, " ----has max----", m,c
                    return (m,c)
                bucket.pop(m)
                                    
        states = []
        gainstate = 0

        # iterate through a pass, move at most half the nodes.
        for i in range(self._halftotal):
            #self._printBucketState()
            print "left:", " ".join(map(lambda c: c.name+":"+str(c.biPartChain[1]), self.sets[0]))
            print "right:", " ".join(map(lambda c: c.name+":"+str(c.biPartChain[1]), self.sets[1]))
            maxa = getMax(ba)
            maxb = getMax(bb)
            if maxa[0] > maxb[0] and len(ba) > self._limits[0]:
                # move from a to b
                self._moveChainNode(maxa[1], [0,1], (ba,bb))
                gainstate += maxa[0]
            else:
                # move from b to a
                self._moveChainNode(maxb[1], [1,0], (bb,ba))
                gainstate += maxb[0]
            print "sub", gainstate
            self._writeSetState("stage%d_%d"%(self.passcount,i+1))
            states.append((gainstate, self.sets[0].copy(), self.sets[1].copy()))
            #print "(%d, %d)" %(tuple(map(len,self.sets)))
            if not (self._limits[0] < len(self.sets[0]) < self._limits[1]):
                break
        # backtrack and return best state
        print "gainstate",gainstate
        #print "gainstates", states
        return max(states)

    def _printBucketState(self):
        for i in range(len(self._buckets)):
            b = self._buckets[i]
            print "Bucket %d" %i
            items = b.items()
            items.sort()
            print "\n".join(map(lambda i: "%d -> %s" %(i[0],
                                                       str(map(id,i[1]))),
                                items))
            print "size:", reduce(lambda x,y: x + len(y[1]), items, 0)


    def _moveChainNode(self, chain, sets, buckets):
        """When we move a node, it gets locked, so... it doesn't actually
        need to be considered anymore: so we don't have to add it to
        a bucket.  """
        #self._printBucketState()        
        e = chain.pop()
        if e in self._locked:
            print "error! can't move locked node."
            
        self.sets[sets[0]].remove(e)
        self.sets[sets[1]].add(e)
        #remove myself from my bucket
        self._buckets[e.biPartBuckets[0]][e.biPartChain[1]]
        #e.biPartBuckets = [e.biPartBuckets[1],e.biPartBuckets[0]]
        e.biPartBuckets.reverse()
        e.biPartSets.reverse()

        #e.biPartSets = [e.biPartSets[1],e.biPartSets[0]]

        #find new gain: (We can actually skip this step)
        #self._addToBucket(buckets[1], e, sets[1], sets[0])
        # add moved node to locked set
        self._locked.add(e)
        for n in (e.parents + e.children):
            self._updateBucketNode(n)

        #self._printBucketState()        
        return


    def _updateBucketNode(self, node):
        # find what bucket node we're in.
        mybucket = node.biPartBuckets[0]
        mysets = node.biPartSets
        (bucketnum, gain) = node.biPartChain
        try:
            myChain = self._buckets[bucketnum][gain]
        except:
            print "didn't find",gain,"in",self._buckets[bucketnum]
            if bucketnum == 1: checknum = 0
            else: checknum = 1
            print "maybe in", self._buckets[checknum]
            return
        # we weren't in the chain: not bucketed anymore.
        if node not in myChain:
            assert node in self._locked
            return # don't bother to update.
        # take myself out of the chain
        before = len(myChain)
        myChain.remove(node) #this is O(len(bucket)), w/o double L-L

        if len(myChain) > 10:
            print "chain length > 10:", len(myChain)
        self._addToBucket(mybucket, node, mysets[0], mysets[1])
        # 
       
    def _addToBucket(self, bucketnum, cmd, a, b):
        bucket = self._buckets[bucketnum]
        gain = self._findGain(cmd, a,b)
        #print "looking for gain",gain, bucket[gain]
        bucket[gain] = bucket.get(gain,[]) + [cmd]
        #print "newgain",gain, bucket[gain]
        cmd.biPartChain = (bucketnum,gain) # faulty if we put bucket[gain] here

        
    def _makeBuckets(self, sets):
        buck = [{},{}]
        self._buckets = buck
        # add a field to each command to eliminate need for extra table
        def fieldAdder(indices):
            def add(c):
                c.biPartBuckets = indices
                c.biPartSets = indices
            return add
        map(fieldAdder([0,1]), sets[0])
        map(fieldAdder([1,0]), sets[1])

        map(lambda c: self._addToBucket(0, c, 0, 1), sets[0])
        map(lambda c: self._addToBucket(1, c, 1, 0), sets[1])
        return buck


    def _findGain(self, cmd, source, dest):
        # gain(move) = number of cross-partition nets before - after
        # gain approximates the benefit from moving a command.
        # so, gain = -delta(cost) = -(cost_after - cost_bfore)
        # = cost_before - cost_after
        
        # before: num of neighbors in other part
        # after: num of neighbors in current part
        # assume cost=1, but can estimate later
        ###source = self._buckets[cmd.biPartBuckets[0]]
        beforecount = len(filter(lambda x: x in self.sets[dest],
                                 cmd.parents + cmd.children))
        aftercount = len(cmd.parents)+len(cmd.children) - beforecount
        print cmd.name, source,dest, "b,a", beforecount, aftercount
        #print "parents", " ".join(map(lambda x:x.name,cmd.parents))
        #print "children", " ".join(map(lambda x:x.name,cmd.children))

        cmd.bipartGain = beforecount-aftercount
        return beforecount - aftercount

        
        pass

    def _writeSetState(self,label):
        rec = set()
        stage0 = [
            "digraph %s {" % label,
            "subgraph clustera { ",
            ";\n".join(map(lambda c:'"%s"'%c.name, self.sets[0])),
            "}",
            "subgraph clusterb { ",
            ";\n".join(map(lambda c:'"%s"'%c.name, self.sets[1])),
            "}",
            str(statDagGraphCmds(self.sets[0], rec)),
            str(statDagGraphCmds(self.sets[1], rec)),
            "}"]
        open("%s.dot" %label,"w").write("\n".join(stage0))
        pass
    
    def result(self):
        return self.sets[0],self.sets[1]

class FakeCmd:
    def __init__(self, name="Noname", parents=[], children=[]):
        self.name = name
        self.parents = parents # Link myself to my parents
        self.outputs = []
        self.inputs = []
        self.oname = name+"_output"
        for c in parents: # Link each parent to me
            oname = c.name+"_output"
            self.inputs.append(oname)
            if oname not in c.outputs:
                c.outputs.append(oname)
            c.children.append(self)
        
        self.children = children # Link me to my children
        for c in children: # Link each child to me
            if not self.outputs:
                self.outputs.append(self.oname)
            c.outputs.append(self.oname)
            c.parents.append(self) 
        pass

def makeIpccIter(prefix="", parents=[]):
    a = FakeCmd(name=prefix+"a", parents=parents, children=[])
    b = FakeCmd(name=prefix+"b", parents=[a], children=[])
    c = FakeCmd(name=prefix+"c", parents=[a,b], children=[])
    return [a,b,c]

def makeFakeCmds():
    clist = []
    one = makeIpccIter(prefix="1")
    two = makeIpccIter(prefix="2")
    three = makeIpccIter(prefix="3")
    ens = makeIpccIter(prefix="E", parents=[one[0], two[0], three[0]])
    map(lambda l: clist.extend(l), [one,two,three,ens])
    return clist

def selftest():
    cl = makeFakeCmds()
    import random # randomize list order
    random.shuffle(cl)
    bp = Bipartitioner(cl)
    s0 = bp.sets[0]
    s1 = bp.sets[1]
    #bp2 = Bipartitioner(map(lambda x:x, s0))

if __name__ == '__main__':
    selftest()
