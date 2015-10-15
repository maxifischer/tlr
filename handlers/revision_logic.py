import hashlib
import zlib

from models import User, Token, Repo, HMap, CSet, Blob
from peewee import IntegrityError, SQL, fn

# This factor (among others) determines whether a snapshot is stored rather
# than a delta, depending on the size of the latest snapshot and subsequent
# deltas. For the latest snapshot `base` and deltas `d1`, `d2`, ..., `dn`
# a new snapshot is definitely stored if:
#
# `SNAPF * len(base) <= len(d1) + len(d2) + ... + len(dn)`
#
# In short, larger values will result in longer delta chains and likely reduce
# storage size at the expense of higher revision reconstruction costs.
#
# TODO: Empirically determine a good value with real data/statistics.
SNAPF = 10.0


def compress(s):
    return zlib.compress(s)

def decompress(s):
    return zlib.decompress(s)

def shasum(s):
    return hashlib.sha1(s).digest()

    
# Parse serialized RDF:
#
# RDF/XML:      application/rdf+xml
# N-Triples:    application/n-triples
# Turtle:       text/turtle
def parse(s, fmt):
    stmts = set()
    parser = RDF.Parser(mime_type=fmt)
    for st in parser.parse_string_as_stream(s, "urn:x-default:tailr"):
        stmts.add(str(st) + " .")
    return stmts
    
def load_repo(username, reponame):
    try:
        repo = (Repo
            .select(Repo.id)
            .join(User)
            .where((User.name == username) & (Repo.name == reponame))
            .naive()
            .get())
    except Repo.DoesNotExist:
        repo = None
    return repo
    
def get_shasum(key):
    sha = shasum(key.encode("utf-8")) #hashing
    return sha
    
def get_chain(repo, sha, ts):
    # Fetch all relevant changes from the last "non-delta" onwards,
    # ordered by time. The returned delta-chain consists of either:
    # a snapshot followed by 0 or more deltas, or
    # a single delete.
    chain = list(CSet
        .select(CSet.time, CSet.type)
        .where(
            (CSet.repo == repo) &
            (CSet.hkey == sha) &
            (CSet.time <= ts) &
            (CSet.time >= SQL(
                "COALESCE((SELECT time FROM cset "
                "WHERE repo_id = %s "
                "AND hkey_id = %s "
                "AND time <= %s "
                "AND type != %s "
                "ORDER BY time DESC "
                "LIMIT 1), 0)",
                repo.id, sha, ts, CSet.DELTA
            )))
        .order_by(CSet.time)
        .naive())
    if len(chain) == 0:
        # A resource does not exist for the given key.
        return None
    return chain

def get_chain_without_time(repo, sha):
    chain = list(CSet
            .select(CSet.time, CSet.type, CSet.len)
            .where(
                (CSet.repo == repo) &
                (CSet.hkey == sha) &
                (CSet.time >= SQL(
                    "COALESCE((SELECT time FROM cset "
                    "WHERE repo_id = %s "
                    "AND hkey_id = %s "
                    "AND type != %s "
                    "ORDER BY time DESC "
                    "LIMIT 1), 0)",
                    repo.id, sha, CSet.DELTA
                )))
            .order_by(CSet.time)
            .naive())
    return chain

def create_blobs(repo, sha, chain):
    blobs = (Blob
        .select(Blob.data)
        .where(
            (Blob.repo == repo) &
            (Blob.hkey == sha) &
            (Blob.time << map(lambda e: e.time, chain)))
        .order_by(Blob.time)
        .naive())
    return blobs

def load_blobs(repo, sha, chain):
    # Load the data required in order to restore the resource state.    
    blobs = create_blobs(repo, sha, chain)
    
    if len(chain) == 1:
            # Special case, where we can simply return
            # the blob data of the snapshot.
            snap = blobs.first().data
            raise ValueError(decompress(snap))
            
    # Recreate the resource for the given key in its latest state -
    # if no `datetime` was provided - or in the state it was in at
    # the time indicated by the passed `datetime` argument. 

    # Load the data required in order to restore the resource state.

    stmts = set()

    for i, blob in enumerate(blobs.iterator()):
        data = decompress(blob.data)

        if i == 0:
            # Base snapshot for the delta chain
            stmts.update(data.splitlines())
        else:
            for line in data.splitlines():
                mode, stmt = line[0], line[2:]
                if mode == "A":
                    stmts.add(stmt)
                else:
                    stmts.discard(stmt)
    return stmts
    
def create_cset(repo, sha):
    # Generate a timemap containing historic change information
    # for the requested key. The timemap is in the default link-format
    # or as JSON (http://mementoweb.org/guide/timemap-json/).
    csets = (CSet
        .select(CSet.time)
        .where((CSet.repo == repo) & (CSet.hkey == sha))
        .order_by(CSet.time.desc())
        .naive())
        
    csit = csets.iterator()
    return csit 
    
def generate_index(repo, ts, page):
    # Subquery for selecting max. time per hkey group
    mx = (CSet
        .select(CSet.hkey, fn.Max(CSet.time).alias("maxtime"))
        .where((CSet.repo == repo) & (CSet.time <= ts))
        .group_by(CSet.hkey)
        .order_by(CSet.hkey)
        .paginate(page, INDEX_PAGE_SIZE)
        .alias("mx"))

    # Query for all the relevant csets (those with max. time values)
    cs = (CSet
        .select(CSet.hkey, CSet.time)
        .join(mx, on=(
            (CSet.hkey == mx.c.hkey_id) &
            (CSet.time == mx.c.maxtime)))
        .where((CSet.repo == repo) & (CSet.type != CSet.DELETE))
        .alias("cs"))

    # Join with the hmap table to retrieve the plain key values
    hm = (HMap
        .select(HMap.val)
        .join(cs, on=(HMap.sha == cs.c.hkey_id))
        .naive())
        
    return hm.iterator()    
    
def detect_collisions(chain, sha, ts, key):
    if len(chain) > 0 and not ts > chain[-1].time:
            # Appended timestamps must be monotonically increasing!
            raise ValueError
    if len(chain) == 0:
        # Mapping for `key` likely does not exist:
        # Store the SHA-to-KEY mapping in HMap,
        # looking out for possible collisions.
        try:
            HMap.create(sha=sha, val=key)
        except IntegrityError:
            val = HMap.select(HMap.val).where(HMap.sha == sha).scalar()
            if val != key:
                raise IntegrityError

def reconstruct_prev_state(chain, repo, sha, stmts, patch, snapc, ts):
    if len(chain) == 0 or chain[0].type == CSet.DELETE:
        # Provide dummy value for `patch` which is never stored.
        # If we get here, we always store a snapshot later on!
        patch = ""
    else:
        # Reconstruct the previous state of the resource
        prev = set()

        blobs = create_blobs(repo, sha, chain)

        load_blobs(repo, sha, chain)

        if stmts == prev:
            # No changes, nothing to be done. Bail out.
            return None

        patch = compress(join(
            map(lambda s: "D " + s, prev - stmts) +
            map(lambda s: "A " + s, stmts - prev), "\n"))

    # Calculate the accumulated size of the delta chain including
    # the (potential) patch from the previous to the pushed state.
    acclen = reduce(lambda s, e: s + e.len, chain[1:], 0) + len(patch)

    blen = len(chain) > 0 and chain[0].len or 0 # base length

    if (len(chain) == 0 or chain[0].type == CSet.DELETE or
        len(snapc) <= len(patch) or SNAPF * blen <= acclen):
        # Store the current state as a new snapshot
        Blob.create(repo=repo, hkey=sha, time=ts, data=snapc)
        CSet.create(repo=repo, hkey=sha, time=ts, type=CSet.SNAPSHOT,
            len=len(snapc))
    else:
        # Store a directed delta between the previous and current state
        Blob.create(repo=repo, hkey=sha, time=ts, data=patch)
        CSet.create(repo=repo, hkey=sha, time=ts, type=CSet.DELTA,
            len=len(patch))
    return 0
                
def create_last_set(sha, repo):
    try:
        last = (CSet
            .select(CSet.time, CSet.type)
            .where((CSet.repo == repo) & (CSet.hkey == sha))
            .order_by(CSet.time.desc())
            .limit(1)
            .naive()
            .get())
    except CSet.DoesNotExist:
        last = None
    return last
        # No changeset was found for the given key -
        # the resource does not exist.

def insert_delete_changes(repo, last, ts):
    # Insert the new "delete" change.
    CSet.create(repo=repo, hkey=last[3], time=ts, type=CSet.DELETE, len=0)