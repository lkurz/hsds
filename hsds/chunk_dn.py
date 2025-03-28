##############################################################################
# Copyright by The HDF Group.                                                #
# All rights reserved.                                                       #
#                                                                            #
# This file is part of HSDS (HDF5 Scalable Data Service), Libraries and      #
# Utilities.  The full HSDS copyright notice, including                      #
# terms governing use, modification, and redistribution, is contained in     #
# the file COPYING, which can be found at the root of the source code        #
# distribution tree.  If you do not have access to this file, you may        #
# request a copy from help@hdfgroup.org.                                     #
##############################################################################
#
# value operations
# handles regauests to read/write chunk data
#

import numpy as np
import traceback
from aiohttp.web_exceptions import HTTPBadRequest, HTTPInternalServerError
from aiohttp.web_exceptions import HTTPNotFound, HTTPServiceUnavailable
from aiohttp.web import json_response, StreamResponse

from .util.httpUtil import request_read, getContentType
from .util.arrayUtil import bytesToArray, arrayToBytes, getBroadcastShape
from .util.idUtil import getS3Key, validateInPartition, isValidUuid
from .util.storUtil import isStorObj, deleteStorObj
from .util.hdf5dtype import createDataType, getSubType
from .util.dsetUtil import getSelectionList, getChunkLayout, getShapeDims
from .util.dsetUtil import getSelectionShape, getChunkInitializer
from .util.chunkUtil import getChunkIndex, getDatasetId, chunkQuery
from .util.chunkUtil import chunkWriteSelection, chunkReadSelection
from .util.chunkUtil import chunkWritePoints, chunkReadPoints
from .util.domainUtil import isValidBucketName
from .util.boolparser import BooleanParser
from .datanode_lib import get_metadata_obj, get_chunk, save_chunk

from . import hsds_logger as log
from . import config


async def PUT_Chunk(request):
    """
    Update the requested chunk/selection
    """
    log.request(request)
    app = request.app
    params = request.rel_url.query
    query = None
    query_update = None
    limit = 0
    bucket = None
    input_arr = None
    element_count = None

    if "query" in params:
        query = params["query"]
        log.info(f"PUT_Chunk query: {query}")
    if "Limit" in params:
        limit = int(params["Limit"])
    chunk_id = request.match_info.get("id")
    if not chunk_id:
        msg = "Missing chunk id"
        log.error(msg)
        raise HTTPBadRequest(reason=msg)

    if not isValidUuid(chunk_id, "Chunk"):
        msg = f"Invalid chunk id: {chunk_id}"
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)

    log.debug(f"PUT_Chunk - id: {chunk_id}")

    if not request.has_body:
        msg = "PUT Value with no body"
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)

    if "bucket" in params:
        bucket = params["bucket"]
        log.debug(f"PUT_Chunk using bucket: {bucket}")

    if not bucket:
        msg = "PUT_Chunk - bucket is None"
        log.warn(msg)
        raise HTTPInternalServerError(reason=msg)
    elif not isValidBucketName(bucket):
        msg = f"Invalid bucket name: {bucket}"
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)

    if "element_count" in params:
        try:
            element_count = int(params["element_count"])
        except ValueError:
            msg = "invalid element_count"
            log.warn(msg)
            raise HTTPBadRequest(reason=msg)
        log.debug(f"element_count param: {element_count}")

    try:
        validateInPartition(app, chunk_id)
    except KeyError:
        msg = f"invalid partition for obj id: {chunk_id}"
        log.error(msg)
        raise HTTPInternalServerError()

    if "dset" in params:
        msg = "Unexpected param dset in PUT request"
        log.error(msg)
        raise HTTPBadRequest(reason=msg)

    if "fields" in params:
        select_fields = params["fields"].split(":")
        log.debug(f"PUT_Chunk - got fields: {select_fields}")
    else:
        select_fields = []
        log.debug("PUT_Chunk - no select fields")

    # verify we have at least min_chunk_size free in the chunk cache
    # otherwise, have the client try a bit later
    chunk_cache = app["chunk_cache"]
    min_chunk_size = int(config.get("min_chunk_size"))
    if chunk_id not in chunk_cache and chunk_cache.memFree < min_chunk_size:
        log.warn(f"PUT_Chunk {chunk_id} - not enough room in chunk cache - return 503 ")
        raise HTTPServiceUnavailable()

    dset_id = getDatasetId(chunk_id)

    dset_json = await get_metadata_obj(app, dset_id, bucket=bucket)

    # TBD - does this work with linked datasets?
    dims = getChunkLayout(dset_json)
    rank = len(dims)

    type_json = dset_json["type"]
    dset_dt = createDataType(type_json)
    if select_fields:
        select_dt = getSubType(dset_dt, select_fields)
    else:
        select_dt = dset_dt

    if "size" in type_json:
        itemsize = type_json["size"]
    else:
        itemsize = "H5T_VARIABLE"

    # TBD - cancel pending read if present?

    # get chunk selection from query params
    if "select" in params:
        select = params["select"]
        log.debug(f"PUT_Chunk got select param: {select}")
    else:
        select = None  # put for entire dataspace
    try:
        selection = getSelectionList(select, dims)
    except ValueError as ve:
        log.error(f"ValueError for select: {select}: {ve}")
        raise HTTPInternalServerError()
    log.debug(f"PUT_Chunk slices: {selection}")

    mshape = getSelectionShape(selection)
    if element_count is not None:
        bcshape = getBroadcastShape(mshape, element_count)
        log.debug(f"using bcshape: {bcshape}")
    else:
        bcshape = None

    if bcshape:
        num_elements = np.prod(bcshape)
    else:
        num_elements = np.prod(mshape)

    if getChunkInitializer(dset_json):
        chunk_init = True
    elif query:
        chunk_init = False  # don't initialize new chunks on query update
    else:
        chunk_init = True

    kwargs = {"bucket": bucket, "chunk_init": chunk_init}
    chunk_arr = await get_chunk(app, chunk_id, dset_json, **kwargs)
    is_dirty = False
    if chunk_arr is None:
        if chunk_init:
            log.error("failed to create numpy array")
            raise HTTPInternalServerError()
        else:
            log.warn(f"chunk {chunk_id} not found")
            raise HTTPNotFound()

    if query:
        if not dset_dt.fields:
            log.error("expected compound dtype for PUT query")
            raise HTTPInternalServerError()
        if rank != 1:
            log.error("expected one-dimensional array for PUT query")
            raise HTTPInternalServerError()

        try:
            parser = BooleanParser(query)
        except Exception as e:
            msg = f"query: {query} is not valid, got exception: {e}"
            log.error(msg)
            raise HTTPInternalServerError()
        try:
            eval_str = parser.getEvalStr()
        except Exception as e:
            msg = f"query: {query} unable to get eval str, got exception: {e}"
            log.error(msg)
            raise HTTPInternalServerError()
        log.debug(f"got eval str: {eval_str} for query: {query}")

        query_update = await request.json()
        if not query_update:
            log.warn("PUT_Chunk with query but no query update")
            raise HTTPBadRequest()
        log.debug(f"query_update: {query_update}")
        # TBD - send back binary response to SN node
        try:
            kwargs = {
                "chunk_id": chunk_id,
                "chunk_layout": dims,
                "chunk_arr": chunk_arr,
                "slices": selection,
                "query": eval_str,
                "query_update": query_update,
                "limit": limit,
            }
            rsp_arr = chunkQuery(**kwargs)
            log.debug(f"query_update returned: {len(rsp_arr)} rows")
        except TypeError as te:
            log.warn(f"chunkQuery - TypeError: {te}")
            raise HTTPBadRequest()
        except ValueError as ve:
            log.warn(f"chunkQuery - ValueError: {ve}")
            raise HTTPBadRequest()
        num_hits = rsp_arr.shape[0]
        if num_hits > 0:
            is_dirty = True
            # save chunk
            save_chunk(app, chunk_id, dset_json, chunk_arr, bucket=bucket)
            status_code = 201
        # stream back response array
        read_resp = arrayToBytes(rsp_arr)

        try:
            resp = StreamResponse()
            resp.headers["Content-Type"] = "application/octet-stream"
            resp.content_length = len(read_resp)
            await resp.prepare(request)
            await resp.write(read_resp)
        except Exception as e:
            log.error(f"Exception during binary data write: {e}")
            raise HTTPInternalServerError()
        finally:
            await resp.write_eof()
        return
    else:
        # regular chunk update
        # check that the content_length is what we expect
        if itemsize != "H5T_VARIABLE":
            log.debug(f"expected content_length: {num_elements * itemsize}")
        log.debug(f"actual content_length: {request.content_length}")

        actual = request.content_length
        if itemsize != "H5T_VARIABLE":
            expected = num_elements * itemsize
            if expected % actual != 0:
                msg = f"Expected content_length of: {expected}, but got: {actual}"
                log.error(msg)
                raise HTTPBadRequest(reason=msg)

        # create a numpy array for incoming data
        input_bytes = await request_read(request)
        # TBD - will it cause problems when failures are raised before
        #    reading data?
        if len(input_bytes) != actual:
            msg = f"Read {len(input_bytes)} bytes, expecting: {actual}"
            log.error(msg)
            raise HTTPInternalServerError()

        try:
            input_arr = bytesToArray(input_bytes, select_dt, [num_elements, ])
        except ValueError as ve:
            log.error(f"bytesToArray threw ValueError: {ve}")
            tb = traceback.format_exc()
            log.error(f"traceback: {tb}")

            raise HTTPBadRequest(reason="unable to decode bytestring")

        if bcshape:
            input_arr = input_arr.reshape(bcshape)
            log.debug(f"broadcasting {bcshape} to mshape {mshape}")
            arr_tmp = np.zeros(mshape, dtype=select_dt)
            arr_tmp[...] = input_arr
            input_arr = arr_tmp
        else:
            input_arr = input_arr.reshape(mshape)

        kwargs = {"chunk_arr": chunk_arr, "slices": selection, "data": input_arr}
        is_dirty = chunkWriteSelection(**kwargs)

        # chunk update successful
        resp = {}
    if is_dirty or config.get("write_zero_chunks", default=False):
        save_chunk(app, chunk_id, dset_json, chunk_arr, bucket=bucket)
        status_code = 201
    else:
        status_code = 200

    resp = json_response(resp, status=status_code)
    log.response(request, resp=resp)
    return resp


async def GET_Chunk(request):
    """
    Return data from requested chunk and selection
    """
    log.request(request)

    bucket = None
    s3path = None
    s3offset = None
    s3size = None
    hyper_dims = None
    dims = None
    query = None
    limit = 0

    app = request.app
    params = request.rel_url.query

    chunk_id = request.match_info.get("id")
    if not chunk_id:
        msg = "Missing chunk id"
        log.error(msg)
        raise HTTPBadRequest(reason=msg)
    if not isValidUuid(chunk_id, "Chunk"):
        msg = f"Invalid chunk id: {chunk_id}"
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)

    try:
        validateInPartition(app, chunk_id)
    except KeyError:
        msg = f"invalid partition for obj id: {chunk_id}"
        log.error(msg)
        raise HTTPInternalServerError()

    if "s3path" in params:
        s3path = params["s3path"]
        log.debug(f"GET_Chunk - using URI: {s3path}")
    if "bucket" in params:
        bucket = params["bucket"]
    if not bucket:
        msg = "GET_Chunk - bucket is None"
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)
    elif not isValidBucketName(bucket):
        msg = f"Invalid bucket name: {bucket}"
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)

    log.debug(f"GET_Chunk - using bucket: {bucket}")

    if "s3offset" in params:
        param_s3offset = params["s3offset"]
        try:
            if param_s3offset.find(":") > 0:
                # colon seperated index values, convert to list
                s3offset = list(map(int, param_s3offset.split(":")))
            else:
                s3offset = int(param_s3offset)
        except ValueError:
            log.error(f"invalid s3offset params: {param_s3offset}")
            raise HTTPBadRequest()
        log.debug(f"s3offset: {s3offset}")

    if "s3size" in params:
        param_s3size = params["s3size"]
        try:
            if param_s3size.find(":") > 0:
                s3size = list(map(int, param_s3size.split(":")))
            else:
                s3size = int(param_s3size)
        except ValueError:
            log.error(f"invalid s3size params: {param_s3size}")
            raise HTTPBadRequest()
        log.debug(f"s3size: {s3size}")

    if "hyper_dims" in params:
        param_hyper_dims = params["hyper_dims"]
        try:
            if param_hyper_dims.find(":") > 0:
                hyper_dims = list(map(int, param_hyper_dims.split(":")))
            else:
                hyper_dims = [int(param_hyper_dims), ]
        except ValueError:
            log.error(f"invalid hyper_dims params: {param_hyper_dims}")
            raise HTTPBadRequest()
        log.debug(f"hyper_dims: {hyper_dims}")

    if "query" in params:
        query = params["query"]
        log.debug(f"got query: {query}")

    if "Limit" in params:
        param_limit = params["Limit"]
        log.debug(f"limit: {limit}")
        try:
            limit = int(param_limit)
        except ValueError:
            log.error(f"invalid Limit param: {param_limit}")
            raise HTTPBadRequest()

    if s3path:
        # calculate how many chunk bytes we'll read
        num_bytes = 0
        if isinstance(s3size, int):
            num_bytes = s3size
        else:
            # list
            num_bytes = np.sum(s3size)
        log.debug(f"reading {num_bytes} bytes from {s3path}")
        if num_bytes == 0:
            log.warn(f"GET_Chunk for s3path: {s3path} with empty byte range")
            raise HTTPNotFound()

    dset_id = getDatasetId(chunk_id)

    dset_json = await get_metadata_obj(app, dset_id, bucket=bucket)
    shape_dims = getShapeDims(dset_json["shape"])
    log.debug(f"shape_dims: {shape_dims}")
    dims = getChunkLayout(dset_json)
    log.debug(f"GET_Chunk - got dims: {dims}")

    # get chunk selection from query params
    if "select" in params:
        select = params["select"]
    else:
        select = None  # get slices for entire datashape
    if select is not None:
        log.debug(f"GET_Chunk - using select string: {select}")
    else:
        log.debug("GET_Chunk - no selection string")

    try:
        selection = getSelectionList(select, dims)
    except ValueError as ve:
        log.error(f"ValueError for select: {select}: {ve}")
        raise HTTPInternalServerError()
    log.debug(f"GET_Chunk - got selection: {selection}")

    if "fields" in params:
        select_fields = params["fields"].split(":")
        log.debug(f"GET_Chunk - got fields: {select_fields}")
    else:
        select_fields = []

    if getChunkInitializer(dset_json):
        chunk_init = True
    else:
        chunk_init = False

    kwargs = {}
    if s3path:
        kwargs["s3path"] = s3path
        kwargs["s3offset"] = s3offset
        kwargs["s3size"] = s3size
        if hyper_dims:
            kwargs["hyper_dims"] = hyper_dims
    else:
        kwargs["bucket"] = bucket

    kwargs["chunk_init"] = chunk_init

    chunk_arr = await get_chunk(app, chunk_id, dset_json, **kwargs)
    if chunk_arr is None:
        msg = f"chunk {chunk_id} not found"
        log.warn(msg)
        raise HTTPNotFound()

    if chunk_init:
        save_chunk(app, chunk_id, dset_json, chunk_arr, bucket=bucket)

    if select_fields:
        try:
            select_dt = getSubType(chunk_arr.dtype, select_fields)
        except TypeError as te:
            # this shouldn't happen, but just in case...
            msg = f"invalid fields selection: {te}"
            log.warn(msg)
            raise HTTPBadRequest(reason=msg)
    else:
        select_dt = chunk_arr.dtype

    if query:
        # if there's a where clause, just use the expression
        # part with BooleanParser
        # TBD: Remove when BooleanParser knows how to use where keyword
        if query.startswith("where"):
            query_expr = None
        else:
            n = query.find(" where ")
            if n > 0:
                query_expr = query[:n]
            else:
                query_expr = query
        if query_expr:
            try:
                parser = BooleanParser(query_expr)
            except Exception as e:
                msg = f"query: {query} is not valid, got exception: {e}"
                log.error(msg)
                raise HTTPInternalServerError()
            try:
                eval_str = parser.getEvalStr()
            except Exception as e:
                msg = f"query: {query} unable to get eval str, got exception: {e}"
                log.error(msg)
                raise HTTPInternalServerError()
            log.debug(f"got eval str: {eval_str} for query: {query}")

        # run given query
        try:
            kwargs = {
                "chunk_id": chunk_id,
                "chunk_layout": dims,
                "chunk_arr": chunk_arr,
                "slices": selection,
                "query": query,
                "limit": limit,
                "select_dt": select_dt,
            }
            output_arr = chunkQuery(**kwargs)
        except TypeError as te:
            log.warn(f"chunkQuery - TypeError: {te}")
            raise HTTPBadRequest()
        except ValueError as ve:
            log.warn(f"chunkQuery - ValueError: {ve}")
            raise HTTPBadRequest()
        if output_arr is None or output_arr.shape[0] == 0:
            # no matches to query
            msg = f"chunk {chunk_id} no results for query: {query}"
            log.debug(msg)
            raise HTTPNotFound()
        log.debug(f"test - got output_arr: {output_arr}")
    else:
        # read selected data from chunk
        output_arr = chunkReadSelection(chunk_arr, slices=selection, select_dt=select_dt)

    # write response
    if output_arr is not None:
        log.debug(f"GET_Chunk - returning arr: {output_arr.shape}")
        read_resp = arrayToBytes(output_arr)

        try:
            resp = StreamResponse()
            resp.headers["Content-Type"] = "application/octet-stream"
            resp.content_length = len(read_resp)
            await resp.prepare(request)
            await resp.write(read_resp)
        except Exception as e:
            log.error(f"Exception during binary data write: {e}")
            raise HTTPInternalServerError()
        finally:
            await resp.write_eof()
    else:
        # JSON response
        # TBD: this case should no longer be relevant
        resp = json_response(read_resp)

    return resp


async def POST_Chunk(request):
    """
    Return data from requested chunk and point selection
    """
    log.request(request)
    app = request.app
    params = request.rel_url.query
    content_type = getContentType(request)

    put_points = False
    select = None  # for hyperslab/fancy selection
    body = None
    num_points = 0
    select_fields = None

    if "count" in params:
        try:
            num_points = int(params["count"])
        except ValueError:
            msg = f"expected int for count param but got: {params['count']}"
            log.warn(msg)
            raise HTTPBadRequest(reason=msg)
    else:
        if content_type == "binary":
            msg = "expected count query param for binary POST chunks request"
            log.warn(msg)
            raise HTTPBadRequest(reason=msg)

    if "action" in params and params["action"] == "put":
        log.info(f"POST Chunk put points - num_points: {num_points}")
        put_points = True
    else:
        log.info(f"POST Chunk get points - num_points: {num_points}")

    if "bucket" not in params:
        msg = "POST_Chunk - expected bucket param"
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)

    bucket = params["bucket"]

    if not isValidBucketName(bucket):
        msg = f"Invalid bucket name: {bucket}"
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)

    s3path = None
    s3offset = 0
    s3size = 0
    if "s3path" in params:
        if put_points:
            log.error("s3path can not be used with put points POST request")
            raise HTTPBadRequest()
        s3path = params["s3path"]
        log.debug(f"POST_Chunk - using s3path: {s3path}")

    if "s3offset" in params:
        try:
            s3offset = int(params["s3offset"])
        except ValueError:
            log.error(f"invalid s3offset params: {params['s3offset']}")
            raise HTTPBadRequest()
    if "s3size" in params:
        try:
            s3size = int(params["s3size"])
        except ValueError:
            log.error(f"invalid s3size params: {params['s3size']}")
            raise HTTPBadRequest()

    chunk_id = request.match_info.get("id")
    if not chunk_id:
        msg = "Missing chunk id"
        log.error(msg)
        raise HTTPBadRequest(reason=msg)
    log.info(f"POST chunk_id: {chunk_id}")
    chunk_index = getChunkIndex(chunk_id)
    log.debug(f"chunk_index: {chunk_index}")

    if not isValidUuid(chunk_id, "Chunk"):
        msg = f"Invalid chunk id: {chunk_id}"
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)

    try:
        validateInPartition(app, chunk_id)
    except KeyError:
        msg = f"invalid partition for obj id: {chunk_id}"
        log.error(msg)
        raise HTTPInternalServerError()

    log.debug(f"request params: {list(params.keys())}")
    if "dset" in params:
        msg = "Unexpected dset in POST request"
        log.error(msg)
        raise HTTPBadRequest(reason=msg)

    if not request.has_body:
        msg = "POST Value with no body"
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)

    dset_id = getDatasetId(chunk_id)

    dset_json = await get_metadata_obj(app, dset_id, bucket=bucket)
    log.debug(f"get_metadata_obj for {dset_id} returned {dset_json}")
    dims = getChunkLayout(dset_json)
    rank = len(dims)

    type_json = dset_json["type"]
    dset_dt = createDataType(type_json)
    output_arr = None

    if getChunkInitializer(dset_json):
        chunk_init = True
    elif put_points:
        chunk_init = True
    else:
        # don't need for getting points
        chunk_init = False

    if "fields" in params:
        select_fields = params["fields"].split(":")
    if select_fields:
        select_dt = getSubType(dset_dt, select_fields)
    else:
        select_dt = dset_dt

    if content_type == "binary":
        # create a numpy array for incoming points
        input_bytes = await request_read(request)
        actual = request.content_length
        if len(input_bytes) != actual:
            msg = f"Read {len(input_bytes)} bytes, expecting: {actual}"
            log.error(msg)
            raise HTTPInternalServerError()

        if rank == 1:
            coord_type_str = "uint64"
        else:
            coord_type_str = f"({rank},)uint64"

        if put_points:
            # create a numpy array with the following type:
            #       (coord1, coord2, ...) | dset_dtype
            type_fields = [("coord", np.dtype(coord_type_str)), ("value", select_dt)]
            point_dt = np.dtype(type_fields)
            point_shape = (num_points,)
        else:
            point_dt = np.dtype("uint64")
            point_shape = (num_points, rank)
        point_arr = bytesToArray(input_bytes, point_dt, point_shape)
    else:
        # fancy/hyperslab selection
        body = await request.json()
        if "select" not in body:
            log.warn("expected 'select' key in body of POST_Value request")
            raise HTTPBadRequest()
        select = body["select"]
        log.debug(f"POST_Chunk - using select string: {select}")
        if "fields" in body:
            if select_fields:
                # this should have been caught in the chunk_sn code...
                msg = "POST_Chunk: got fields key in body when already given as query param"
                log.warn(msg)
                raise HTTPBadRequest(reason=msg)

            select_fields = body["fields"]
            if isinstance(select_fields, str):
                select_fields = [select_fields, ]  # convert to a list
            log.debug(f"POST_Chunk - got fields: {select_fields}")
            select_dt = getSubType(dset_dt, select_fields)

    kwargs = {"chunk_init": chunk_init}
    if s3path:
        kwargs["s3path"] = s3path
        kwargs["s3offset"] = s3offset
        kwargs["s3size"] = s3size
    else:
        kwargs["bucket"] = bucket

    chunk_arr = await get_chunk(app, chunk_id, dset_json, **kwargs)
    if chunk_arr is None:
        log.warn(f"chunk {chunk_id} not found")
        raise HTTPNotFound()

    if chunk_init and not put_points:
        # lazily write chunk to storage
        save_chunk(app, chunk_id, dset_json, chunk_arr, bucket=bucket)

    if put_points:
        # writing point data
        try:
            kwargs = {
                "chunk_id": chunk_id,
                "chunk_layout": dims,
                "chunk_arr": chunk_arr,
                "point_arr": point_arr,
                "select_dt": select_dt,
            }
            chunkWritePoints(**kwargs)
        except ValueError as ve:
            log.warn(f"got value error from chunkWritePoints: {ve}")
            raise HTTPBadRequest()
        # lazily write chunk to storage
        save_chunk(app, chunk_id, dset_json, chunk_arr, bucket=bucket)
    elif select:
        # hyperslab/fancy read selection
        try:
            selection = getSelectionList(select, dims)
        except ValueError as ve:
            log.error(f"ValueError for select: {select}: {ve}")
            raise HTTPInternalServerError()
        log.debug(f"GET_Chunk - got selection: {selection}")
        # read selected data from chunk
        output_arr = chunkReadSelection(chunk_arr, slices=selection, select_dt=select_dt)

    else:
        # read points
        try:
            kwargs = {
                "chunk_id": chunk_id,
                "chunk_layout": dims,
                "chunk_arr": chunk_arr,
                "point_arr": point_arr,
                "select_dt": select_dt
            }
            output_arr = chunkReadPoints(**kwargs)
        except ValueError as ve:
            log.warn(f"got value error from chunkReadPoints: {ve}")
            raise HTTPBadRequest()

    if output_arr is None:
        # write empty response
        resp = json_response({})
    else:
        output_data = arrayToBytes(output_arr)
        # write response
        try:
            resp = StreamResponse()
            resp.headers["Content-Type"] = "application/octet-stream"
            resp.content_length = len(output_data)
            await resp.prepare(request)
            await resp.write(output_data)
        except Exception as e:
            log.error(f"Exception during binary data write: {e}")
            raise HTTPInternalServerError()
        finally:
            await resp.write_eof()

    return resp


async def DELETE_Chunk(request):
    """HTTP DELETE method for /chunks/
    """
    log.request(request)
    app = request.app
    params = request.rel_url.query
    chunk_id = request.match_info.get("id")
    if not chunk_id:
        msg = "Missing chunk id"
        log.error(msg)
        raise HTTPBadRequest(reason=msg)
    log.info(f"DELETE chunk: {chunk_id}")

    if not isValidUuid(chunk_id, "Chunk"):
        msg = f"Invalid chunk id: {chunk_id}"
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)
    if "bucket" in params:
        bucket = params["bucket"]

    if not bucket:
        msg = "DELETE_Chunk - bucket param not set"
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)
    elif not isValidBucketName(bucket):
        msg = f"Invalid bucket name: {bucket}"
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)

    try:
        validateInPartition(app, chunk_id)
    except KeyError:
        msg = f"invalid partition for obj id: {chunk_id}"
        log.error(msg)
        raise HTTPInternalServerError()

    chunk_cache = app["chunk_cache"]
    s3key = getS3Key(chunk_id)
    log.debug(f"DELETE_Chunk s3_key: {s3key}")

    if chunk_id in chunk_cache:
        del chunk_cache[chunk_id]

    filter_map = app["filter_map"]
    dset_id = getDatasetId(chunk_id)
    if dset_id in filter_map:
        # The only reason chunks are ever deleted is if the dataset is being
        # deleted, so it should be safe to remove this entry now
        log.info(f"Removing filter_map entry for {dset_id}")
        del filter_map[dset_id]

    if await isStorObj(app, s3key, bucket=bucket):
        await deleteStorObj(app, s3key, bucket=bucket)
    else:
        msg = f"delete_metadata_obj - key {s3key} not found (never written)?"
        log.info(msg)

    resp_json = {}
    resp = json_response(resp_json)
    log.response(request, resp=resp)
    return resp
