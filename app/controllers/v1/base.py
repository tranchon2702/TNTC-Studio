from fastapi import APIRouter


def new_router(dependencies=None):
    router = APIRouter()
    router.tags = ["V1"]
    router.prefix = "/api/v1"
    # Áp dụng phụ thuộc xác thực cho tất cả các tuyến
    if dependencies:
        router.dependencies = dependencies
    return router
