from flask import request
from flask_restful import Resource, marshal_with  # type: ignore

import services
from controllers.common.errors import FilenameNotExistsError
from controllers.service_api import api
from controllers.service_api.app.error import (
    FileTooLargeError,
    NoFileUploadedError,
    TooManyFilesError,
    UnsupportedFileTypeError,
)
from controllers.service_api.wraps import FetchUserArg, WhereisUserArg, validate_app_token
from fields.file_fields import file_fields
from models.model import ApiToken, App, EndUser  # 二开部分End - 密钥额度限制，新增api_token,否则上传文件会报错
from services.file_service import FileService


class FileApi(Resource):
    @validate_app_token(fetch_user_arg=FetchUserArg(fetch_from=WhereisUserArg.FORM))
    @marshal_with(file_fields)
    def post(self, app_model: App, end_user: EndUser, api_token: ApiToken):  # 二开部分End - 密钥额度限制，新增api_token,否则上传文件会报错
        file = request.files["file"]

        # check file
        if "file" not in request.files:
            raise NoFileUploadedError()

        if not file.mimetype:
            raise UnsupportedFileTypeError()

        if len(request.files) > 1:
            raise TooManyFilesError()

        if not file.filename:
            raise FilenameNotExistsError

        try:
            upload_file = FileService.upload_file(
                filename=file.filename,
                content=file.read(),
                mimetype=file.mimetype,
                user=end_user,
            )
        except services.errors.file.FileTooLargeError as file_too_large_error:
            raise FileTooLargeError(file_too_large_error.description)
        except services.errors.file.UnsupportedFileTypeError:
            raise UnsupportedFileTypeError()

        return upload_file, 201


api.add_resource(FileApi, "/files/upload")
