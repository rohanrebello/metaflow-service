import hashlib
import json
import os
from .utils import decode, batchiter, get_artifact, S3ObjectTooBig
from services.utils import logging
from . import cached

MAX_SIZE = 4096
S3_BATCH_SIZE = 512
TTL = os.environ.get("SEARCH_RESULT_CACHE_TTL_SECONDS", 60 * 60 * 24)  # Default TTL to one day

logger = logging.getLogger('SearchArtifacts')


def cache_search_key(function, session, locations, searchterm, stream_output):
    "cache key generator for search results. Used to keep the cache keys as short as possible"
    uniq_locs = list(frozenset(sorted(loc for loc in locations if isinstance(loc, str))))
    _string = "-".join(uniq_locs) + searchterm
    return "artifactsearch:{}".format(hashlib.sha1(_string.encode('utf-8')).hexdigest())


@cached(ttl=TTL, alias="default", key_builder=cache_search_key)
async def search_artifacts(boto_session, locations, searchterm, stream_output=None):
    '''
        Fetches artifacts by locations and performs a search against the object contents.
        Caches artifacts based on location, and search results based on a combination of query&artifacts searched

        Returns:
            {
                "s3_location": {
                    "included": boolean,
                    "matches": boolean
                }
            }
        matches: determines whether object content matched search term

        included: denotes if the object content was able to be included in the search (accessible or not)
        '''

    # Helper functions for streaming status updates.
    async def stream_progress(num):
        if stream_output:
            await stream_output({"type": "progress", "fraction": num})

    async def stream_error(err, id):
        if stream_output:
            await stream_output({"type": "error", "message": err, "id": id})

    # drop duplicates and non-string locations as inactionable.
    locations = list(frozenset(loc for loc in locations if isinstance(loc, str)))

    # Fetch the S3 locations data
    s3_locations = [loc for loc in locations if loc.startswith("s3://")]
    num_s3_batches = max(1, len(locations) // S3_BATCH_SIZE)
    fetched = {}
    async with boto_session.create_client('s3') as s3_client:
        for current_batch_number, locations in enumerate(batchiter(s3_locations, S3_BATCH_SIZE), start=1):
            try:
                for location in locations:
                    artifact_data = await get_artifact(s3_client, location)  # this should preferrably hit a cache.
                    try:
                        content = decode(artifact_data)
                        fetched[location] = json.dumps([True, content])
                    except TypeError:
                        # In case the artifact was of a type that can not be json serialized,
                        # we try casting it to a string first.
                        fetched[location] = json.dumps([True, str(content)])
                    except S3ObjectTooBig:
                        fetched[location] = json.dumps([False, 'object is too large'])
                    except Exception as ex:
                        # Exceptions might be fixable with configuration changes or other measures,
                        # therefore we do not want to write anything to the cache for these artifacts.
                        logger.exception("exception happened when parsing artifact content")
                        await stream_error(str(ex), "artifact-handle-failed")
                await stream_progress(current_batch_number / num_s3_batches)
            except Exception as ex:
                logger.exception('An exception was encountered while searching.')
                err_id = getattr(ex, "id", "generic-error")
                await stream_error(str(ex), err_id)
    # Skip the inaccessible locations
    other_locations = [loc for loc in locations if not loc.startswith("s3://")]
    for loc in other_locations:
        await stream_error("Artifact is not accessible", "artifact-not-accessible")
        fetched[loc] = json.dumps([False, 'object is not accessible'])

    # Perform search on loaded artifacts.
    search_results = {}

    for key in locations:
        if key in fetched:
            load_success, value = json.loads(fetched[key])
        else:
            load_success, value = False, None

        search_results[key] = {
            "included": load_success,
            "matches": str(value) == searchterm
        }

    return search_results
