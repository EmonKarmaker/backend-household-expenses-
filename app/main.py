"""FastAPI app entry point."""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    yield
    # Shutdown


app = FastAPI(
    title="Household Expense Tracker",
    description="Track rent, utilities, meals, and shared expenses fairly.",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health", tags=["meta"])
async def health() -> dict[str, str]:
    return {"status": "ok", "env": settings.app_env}


from app.routers import assets, auth, bills, comments, deposits, item_catalog, meals, months, rooms, setup, settlements, shopping, users

app.include_router(setup.router,       prefix="/api/v1/setup",       tags=["setup"])
app.include_router(auth.router,        prefix="/api/v1/auth",        tags=["auth"])
app.include_router(rooms.router,       prefix="/api/v1/rooms",       tags=["rooms"])
app.include_router(users.router,       prefix="/api/v1/users",       tags=["users"])
app.include_router(bills.router,       prefix="/api/v1/bills",       tags=["bills"])
app.include_router(shopping.router,    prefix="/api/v1/shopping",    tags=["shopping"])
app.include_router(meals.router,       prefix="/api/v1/meals",       tags=["meals"])
app.include_router(item_catalog.router, prefix="/api/v1/item-catalog", tags=["item-catalog"])
app.include_router(comments.router,    prefix="/api/v1/comments",    tags=["comments"])
app.include_router(months.router,      prefix="/api/v1/months",      tags=["months"])
app.include_router(settlements.router, prefix="/api/v1/settlements", tags=["settlements"])
app.include_router(assets.router,      prefix="/api/v1/assets",      tags=["assets"])
app.include_router(deposits.router,    prefix="/api/v1/deposits",    tags=["deposits"])
